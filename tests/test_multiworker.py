"""
Running more than one process: the scheduler lease, and credit reconciliation.

`ENABLE_SCHEDULER` used to be the coordination — an env var the operator had to
set correctly in every process. Getting it wrong meant two schedulers resetting
quotas and expiring the same keys against each other.
"""
import asyncio
import os
import tempfile

from outline_panel.core.db import DB


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "m.db")


async def _db():
    d = DB(_tmp())
    await d.init()
    return d


# ------------------------------------------------------------------- leases
async def test_only_one_holder_at_a_time():
    db = await _db()
    assert await db.acquire_lease("scheduler", "a", 60) is True
    assert await db.acquire_lease("scheduler", "b", 60) is False
    assert (await db.lease_holder("scheduler"))["holder"] == "a"
    await db.close()


async def test_the_holder_can_renew():
    """Renewal has to work, or the holder loses its own lease mid-run."""
    db = await _db()
    await db.acquire_lease("scheduler", "a", 60)
    assert await db.acquire_lease("scheduler", "a", 60) is True
    await db.close()


async def test_an_expired_lease_is_taken_over():
    """A process that dies stops renewing; nobody is left to clean up after it,
    so the lease has to fall in on its own."""
    db = await _db()
    assert await db.acquire_lease("scheduler", "dead", 0) is True
    assert await db.acquire_lease("scheduler", "alive", 60) is True
    assert (await db.lease_holder("scheduler"))["holder"] == "alive"
    await db.close()


async def test_releasing_only_works_for_the_holder():
    db = await _db()
    await db.acquire_lease("scheduler", "a", 60)
    await db.release_lease("scheduler", "b")           # not yours
    assert (await db.lease_holder("scheduler"))["holder"] == "a"
    await db.release_lease("scheduler", "a")
    assert await db.lease_holder("scheduler") is None
    await db.close()


async def test_a_race_for_the_lease_has_one_winner():
    db = await _db()
    got = await asyncio.gather(*[
        db.acquire_lease("scheduler", f"w{i}", 60) for i in range(10)])
    assert sum(got) == 1
    await db.close()


async def test_two_schedulers_do_not_both_do_the_work(monkeypatch):
    """The whole point: run the loop in every process, work happens once."""
    from outline_panel.core import scheduler
    db = await _db()
    runs = []

    async def spy(registry, database, notifier, notified, settings=None):
        runs.append(1)

    monkeypatch.setattr(scheduler, "_check_once", spy)

    class Reg:
        def get(self, sid):
            return None

    tasks = [asyncio.create_task(
        scheduler.expiry_loop(Reg(), db, 1, holder=f"worker-{i}"))
        for i in range(3)]
    await asyncio.sleep(0.3)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # three loops, one pass — the two without the lease stood by
    assert len(runs) == 1, f"{len(runs)} processes did the work"
    await db.close()


# ---------------------------------------------------------- reconciliation
async def test_no_drift_when_the_ledger_is_the_only_writer():
    db = await _db()
    aid = await db.add_admin("sara", "h", "s")
    await db.update_admin(aid, credit_enabled=1)
    await db.credit_admin(aid, 50_000, reason="topup")
    await db.charge(aid, 20_000, reason="purchase")
    assert await db.credit_drift() == []
    assert (await db.get_admin(aid))["credit"] == await db.ledger_sum(aid)
    await db.close()


async def test_drift_is_detected():
    """Something writing admins.credit outside the ledger is exactly what this
    exists to catch — it was tested once and never watched at runtime."""
    db = await _db()
    aid = await db.add_admin("sara", "h", "s")
    await db.update_admin(aid, credit_enabled=1)
    await db.credit_admin(aid, 50_000, reason="topup")
    await db.conn.execute("UPDATE admins SET credit = 999 WHERE id = ?", (aid,))
    await db.conn.commit()

    drift = await db.credit_drift()
    assert len(drift) == 1
    assert drift[0]["credit"] == 999 and drift[0]["ledger"] == 50_000
    assert drift[0]["username"] == "sara"
    await db.close()


async def test_the_scheduler_reports_drift(monkeypatch):
    from outline_panel.core import scheduler
    from outline_panel.core.settings import SettingsStore
    db = await _db()
    aid = await db.add_admin("sara", "h", "s")
    await db.credit_admin(aid, 1000, reason="topup")
    await db.conn.execute("UPDATE admins SET credit = 7 WHERE id = ?", (aid,))
    await db.conn.commit()

    msgs = []

    async def notifier(text, **kw):
        msgs.append(text)

    class Reg:
        def get(self, sid):
            return None

    await scheduler._check_once(Reg(), db, notifier, set(), SettingsStore(db))
    assert any("Credit mismatch" in m and "sara" in m for m in msgs)
    await db.close()


# ------------------------------------------------- public subscription route
async def test_the_subscription_route_is_rate_limited_and_cached(monkeypatch):
    """It needs no credentials and reaches every configured server on every
    call, so an open loop against one link is an amplifier pointed at the fleet.
    """
    import os
    import sys

    import httpx
    os.environ["DB_PATH"] = _tmp()
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["TRUST_PROXY"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    from outline_panel.web.routers import subscription as sub
    from test_features import FakeOutline
    await deps.db.init()
    await deps.settings.bootstrap()
    fake = FakeOutline()
    deps.reg.servers["s1"] = {"id": "s1", "name": "Tokyo",
                              "api_url": "https://1.2.3.4:1/x",
                              "cert_sha256": None, "api": fake}
    await deps.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    key = await fake.create_key(name="Ali")
    await deps.db.add_key("s1", key["id"], "Ali", None, None)
    await deps.db.set_sub_token("s1", key["id"], "tok")

    hits = {"n": 0}
    real = fake.get_transfer_metrics

    async def counted():
        hits["n"] += 1
        return await real()

    fake.get_transfer_metrics = counted
    sub._cache.clear()

    t = httpx.ASGITransport(app=appmod.app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        hdr = {"Accept": "*/*", "User-Agent": "v2rayNG/1.8"}
        for _ in range(5):
            assert (await c.get("/sub/tok", headers=hdr)).status_code == 200
        assert hits["n"] == 1, "the cache did not absorb the repeats"

        # membership changes must not be cached away
        sub.invalidate("tok")
        assert (await c.get("/sub/tok", headers=hdr)).status_code == 200
        assert hits["n"] == 2

        # and the per-address ceiling still applies
        await deps.settings.set("sub_max_per_minute", "3")
        await deps.db.clear_rate_events("sub:127.0.0.1")
        codes = [(await c.get("/sub/tok", headers=hdr)).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429

    await deps.db.close()


async def test_the_cache_can_be_switched_off():
    """0 means every fetch is live — the same escape hatch metrics_ttl has."""
    import os
    import sys
    os.environ["DB_PATH"] = _tmp()
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import deps
    from outline_panel.web.routers import subscription as sub
    await deps.db.init()
    calls = {"n": 0}

    async def fresh(token, cfg=None):
        calls["n"] += 1
        return {"name": token}

    sub._collect_fresh = fresh
    sub._cache.clear()
    await deps.settings.set("sub_cache_seconds", "0")
    await sub._collect("t")
    await sub._collect("t")
    assert calls["n"] == 2
    await deps.db.close()
