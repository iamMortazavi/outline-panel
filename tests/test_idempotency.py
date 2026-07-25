"""
Retried purchases.

The charge lands before Outline is called, so a request whose *response* is lost
has already cost the admin. The client cannot tell "never arrived" from
"arrived, reply lost"; an Idempotency-Key is what makes the retry safe.
"""
import asyncio
import os
import sys
import tempfile

import httpx
import pytest

from test_features import FakeOutline


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["TRUST_PROXY"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    fake = FakeOutline()
    deps.reg.servers["s1"] = {"id": "s1", "name": "Tokyo",
                              "api_url": "https://1.2.3.4:1/x",
                              "cert_sha256": None, "api": fake}
    await deps.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    yield appmod.app, deps, fake
    await deps.db.close()


async def _client(application, username="admin", password="pw"):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    r = await c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


async def _reseller(deps, credit=100_000, discount=0):
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    aid = await deps.db.add_admin("sara", h, s,
                                  caps="keys.view,keys.create,keys.edit",
                                  servers="s1")
    await deps.db.update_admin(aid, credit_enabled=1, discount_pct=discount)
    await deps.db.credit_admin(aid, credit, reason="topup")
    return aid


async def _package(deps, price=25_000):
    return await deps.db.add_package("30GB", 30, 30, price)


async def test_a_repeated_purchase_charges_once_and_creates_one_key(app):
    """The bug, directly: without the key this leaves two keys and two charges."""
    application, deps, fake = app
    aid = await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    body = {"name": "Ali", "package_id": pid}
    hdr = {"Idempotency-Key": "buy-once"}
    first = await c.post("/api/servers/s1/keys", json=body, headers=hdr)
    second = await c.post("/api/servers/s1/keys", json=body, headers=hdr)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"
    assert (await deps.db.get_admin(aid))["credit"] == 100_000 - 25_000
    assert len(await deps.db.all_keys()) == 1
    assert len(fake.keys) == 1
    await c.aclose()


async def test_without_a_key_the_old_behaviour_stands(app):
    """Proves the test above is testing the header, not something else — and
    that a client which never sends one still works."""
    application, deps, fake = app
    aid = await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    body = {"name": "Ali", "package_id": pid}
    await c.post("/api/servers/s1/keys", json=body)
    await c.post("/api/servers/s1/keys", json=body)

    assert (await deps.db.get_admin(aid))["credit"] == 100_000 - 2 * 25_000
    assert len(await deps.db.all_keys()) == 2
    await c.aclose()


async def test_concurrent_retries_do_not_both_run(app):
    """Two tabs, one key: the loser is told to retry rather than being served a
    guess. The INSERT decides it, not a check-then-act."""
    application, deps, _ = app
    aid = await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    hdr = {"Idempotency-Key": "race"}
    body = {"name": "Ali", "package_id": pid}
    rs = await asyncio.gather(*[
        c.post("/api/servers/s1/keys", json=body, headers=hdr) for _ in range(6)])
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1
    assert all(x in (200, 409) for x in codes)
    assert (await deps.db.get_admin(aid))["credit"] == 100_000 - 25_000
    assert len(await deps.db.all_keys()) == 1
    await c.aclose()


async def test_the_same_key_with_a_different_body_is_refused(app):
    """Replaying the first answer would be a lie; running the second would break
    the promise the key makes."""
    application, deps, _ = app
    await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    hdr = {"Idempotency-Key": "reused"}
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "package_id": pid},
                 headers=hdr)
    r = await c.post("/api/servers/s1/keys",
                     json={"name": "Someone else", "package_id": pid}, headers=hdr)
    assert r.status_code == 422
    assert len(await deps.db.all_keys()) == 1
    await c.aclose()


async def test_a_failed_purchase_can_be_retried(app):
    """A sale that failed charged nothing (the charge is reversed), so the key
    must not be burned — otherwise a transient Outline error locks the admin out
    of that purchase entirely."""
    application, deps, fake = app
    aid = await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    from outline_panel.core.outline_api import OutlineError
    calls = {"n": 0}
    real = fake.create_key

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutlineError("server unreachable")
        return await real(*a, **kw)

    fake.create_key = flaky
    hdr = {"Idempotency-Key": "retry-me"}
    body = {"name": "Ali", "package_id": pid}

    bad = await c.post("/api/servers/s1/keys", json=body, headers=hdr)
    assert bad.status_code == 502
    assert (await deps.db.get_admin(aid))["credit"] == 100_000, "charge not reversed"

    good = await c.post("/api/servers/s1/keys", json=body, headers=hdr)
    assert good.status_code == 200
    assert (await deps.db.get_admin(aid))["credit"] == 100_000 - 25_000
    assert len(await deps.db.all_keys()) == 1
    await c.aclose()


async def test_a_repeated_renewal_extends_once(app):
    """Renewal charges too, and stacks days on the key."""
    application, deps, fake = app
    aid = await _reseller(deps)
    pid = await _package(deps)
    c = await _client(application, "sara", "sara-pw")

    made = await c.post("/api/servers/s1/keys",
                        json={"name": "Ali", "package_id": pid},
                        headers={"Idempotency-Key": "create"})
    kid = made.json()["id"]
    after_create = (await deps.db.get_admin(aid))["credit"]

    hdr = {"Idempotency-Key": "renew-once"}
    body = {"package_id": pid}
    r1 = await c.post(f"/api/servers/s1/keys/{kid}/extend", json=body, headers=hdr)
    r2 = await c.post(f"/api/servers/s1/keys/{kid}/extend", json=body, headers=hdr)
    assert r1.status_code == 200 and r2.status_code == 200
    assert (await deps.db.get_admin(aid))["credit"] == after_create - 25_000
    await c.aclose()


async def test_a_repeated_free_extension_adds_the_days_once(app):
    """No money here, but a double-send still stacks 60 days onto a 30-day
    renewal — the customer gets what they did not buy."""
    application, deps, fake = app
    c = await _client(application)
    key = await fake.create_key(name="Ali")
    await deps.db.add_key("s1", key["id"], "Ali", None, 30)
    # Far future on purpose: an already-expired key extends from *now*, which is
    # correct but makes the arithmetic depend on the clock rather than on how
    # many times the request ran.
    future = 2_000_000_000
    await deps.db.activate("s1", key["id"], 1_700_000_000, future)

    hdr = {"Idempotency-Key": "plus30"}
    for _ in range(3):
        r = await c.post(f"/api/servers/s1/keys/{key['id']}/extend",
                         json={"days": 30}, headers=hdr)
        assert r.status_code == 200
    row = await deps.db.get_key("s1", key["id"])
    assert row["expiry_ts"] == future + 30 * 86400, "the days stacked up"
    await c.aclose()


async def test_without_a_key_the_days_do_stack(app):
    """Confirms the assertion above is measuring the header and not a no-op."""
    application, deps, fake = app
    c = await _client(application)
    key = await fake.create_key(name="Ali")
    await deps.db.add_key("s1", key["id"], "Ali", None, 30)
    future = 2_000_000_000
    await deps.db.activate("s1", key["id"], 1_700_000_000, future)
    for _ in range(3):
        await c.post(f"/api/servers/s1/keys/{key['id']}/extend", json={"days": 30})
    row = await deps.db.get_key("s1", key["id"])
    assert row["expiry_ts"] == future + 3 * 30 * 86400
    await c.aclose()


async def test_one_admins_key_cannot_collide_with_anothers(app):
    """Keys are client-chosen strings; two admins picking "1" must not share a
    reservation, and one must not be able to probe the other's."""
    application, deps, fake = app
    await _reseller(deps)
    pid = await _package(deps)
    owner = await _client(application)
    sara = await _client(application, "sara", "sara-pw")

    hdr = {"Idempotency-Key": "1"}
    a = await owner.post("/api/servers/s1/keys",
                         json={"name": "owners"}, headers=hdr)
    b = await sara.post("/api/servers/s1/keys",
                        json={"name": "saras", "package_id": pid}, headers=hdr)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] != b.json()["id"]
    assert len(await deps.db.all_keys()) == 2
    await owner.aclose()
    await sara.aclose()


async def test_the_scheduler_clears_old_reservations(app):
    """The table only grows otherwise, and a day-old key replaying is worse than
    letting the retry through."""
    application, deps, _ = app
    import outline_panel.core.db as dbmod
    from outline_panel.core import scheduler

    await deps.db.claim_idempotency("old", 1, "/buy", "h")
    await deps.db.finish_idempotency("old", 200, "{}")
    monkey = dbmod.time.time
    dbmod.time.time = lambda: monkey() + 10 * 86400
    try:
        await scheduler._check_once(deps.reg, deps.db, None, set(), deps.settings)
    finally:
        dbmod.time.time = monkey
    assert await deps.db.claim_idempotency("old", 1, "/buy", "h") is None
