"""
Regressions for the fan-out and lookup work.

Every test here names a cost that used to grow with the size of the panel —
with the number of servers, or with the number of keys — and pins it flat. They
are written so that reverting the fix fails them, not so that they measure
anything absolute: the timing assertions compare concurrent against sequential
with a wide margin, never against a wall-clock budget.
"""
import asyncio
import os
import sys
import tempfile
import time

import pytest

from outline_panel.core.concurrency import map_concurrently
from test_features import FakeOutline

# Long enough to dominate the scheduling noise, short enough that the whole
# file still runs in well under a second.
DELAY = 0.05


class SlowOutline(FakeOutline):
    """A server that answers correctly, slowly. Stands in for the real thing:
    every call here is a TLS round trip to another machine."""

    async def get_key(self, kid):
        await asyncio.sleep(DELAY)
        return await super().get_key(kid)

    async def get_transfer_metrics(self):
        await asyncio.sleep(DELAY)
        return await super().get_transfer_metrics()

    async def get_server_info(self):
        await asyncio.sleep(DELAY)
        return await super().get_server_info()


@pytest.fixture
async def app():
    """The panel with five servers — enough that a sequential fan-out is
    unmistakable next to a concurrent one."""
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    fakes = {}
    for n in range(5):
        sid = f"s{n}"
        f = SlowOutline()
        fakes[sid] = f
        deps.reg.servers[sid] = {"id": sid, "name": f"Server {n}",
                                 "api_url": "https://1.2.3.4:1/x",
                                 "cert_sha256": None, "api": f}
        await deps.db.add_server(sid, f"Server {n}", "https://1.2.3.4:1/x")
    yield appmod.app, deps, fakes
    await deps.db.close()


# ------------------------------------------------------------ the primitive
async def test_map_concurrently_answers_in_the_order_it_was_asked():
    """Callers zip the results back against their inputs, so completion order
    must never leak through."""
    async def slow_for_small(n):
        await asyncio.sleep((5 - n) * 0.01)   # finishes in reverse
        return n

    assert await map_concurrently([0, 1, 2, 3, 4], slow_for_small) == [0, 1, 2, 3, 4]


async def test_map_concurrently_actually_overlaps():
    async def one(_):
        await asyncio.sleep(DELAY)

    started = time.monotonic()
    await map_concurrently(range(10), one)
    assert time.monotonic() - started < 5 * DELAY   # sequential would be 10


async def test_map_concurrently_never_exceeds_its_limit():
    """A panel with fifty servers must not open fifty sockets at once on a
    five-second poll."""
    live = peak = 0

    async def one(_):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    await map_concurrently(range(30), one, limit=4)
    assert peak <= 4


async def test_map_concurrently_handles_the_empty_and_single_cases():
    async def one(n):
        return n * 2

    assert await map_concurrently([], one) == []
    assert await map_concurrently([21], one) == [42]


# --------------------------------------------------------- the subscription
async def test_a_subscription_reaches_every_server_at_once(app):
    """The public link resolved its servers one after another, so a customer on
    five servers waited for five round trips in a row — on the one endpoint
    whose rate their VPN client sets, not an admin."""
    _, deps, fakes = app
    from outline_panel.web.routers import subscription as sub

    for sid, fake in fakes.items():
        key = await fake.create_key(name="Ali")
        await deps.db.add_key(sid, key["id"], "Ali", None, None)
        await deps.db.set_sub_token(sid, key["id"], "tok")

    started = time.monotonic()
    info = await sub._collect_fresh("tok")
    elapsed = time.monotonic() - started

    assert len(info["servers"]) == 5
    # Each server still costs usage-then-key back to back (2 × DELAY); the five
    # servers now overlap, where before they were 10 × DELAY end to end.
    assert elapsed < 5 * DELAY


async def test_a_subscription_keeps_its_servers_in_creation_order(app):
    """Configs are handed to the client in the order the keys were made. Going
    concurrent must not reorder them into whichever server answered first."""
    _, deps, fakes = app
    from outline_panel.web.routers import subscription as sub

    for n, (sid, fake) in enumerate(fakes.items()):
        key = await fake.create_key(name=f"Ali {n}")
        await deps.db.add_key(sid, key["id"], f"Ali {n}", None, None)
        await deps.db.set_sub_token(sid, key["id"], "tok")

    info = await sub._collect_fresh("tok")
    assert [s["server"] for s in info["servers"]] == [f"Server {n}" for n in range(5)]


async def test_one_unreachable_server_costs_a_subscription_only_its_own_config(app):
    """A dead server drops out; the rest of the subscription still resolves."""
    _, deps, fakes = app
    from outline_panel.core.outline_api import OutlineError
    from outline_panel.web.routers import subscription as sub

    for sid, fake in fakes.items():
        key = await fake.create_key(name="Ali")
        await deps.db.add_key(sid, key["id"], "Ali", None, None)
        await deps.db.set_sub_token(sid, key["id"], "tok")

    async def down():
        raise OutlineError("server down")

    fakes["s2"].get_transfer_metrics = down

    info = await sub._collect_fresh("tok")
    assert [s["server"] for s in info["servers"]] == [
        "Server 0", "Server 1", "Server 3", "Server 4"]


# ------------------------------------------------------------- the scheduler
async def test_the_health_probe_reaches_every_server_at_once(app):
    """Probing serially, one unreachable server spent its whole connect timeout
    before the next was tried — delaying expiry enforcement for the fleet."""
    _, deps, _ = app
    from outline_panel.core import scheduler

    started = time.monotonic()
    await scheduler._probe_servers(deps.reg, deps.db, None, set(), deps.settings)
    elapsed = time.monotonic() - started

    assert elapsed < 3 * DELAY            # sequential would be 5 × DELAY
    for sid in deps.reg.ids():            # and every answer was still recorded
        assert (await deps.db.health_history(sid))[0]["reachable"] == 1


# ----------------------------------------------------------------- lookups
async def test_the_subscription_token_lookup_is_indexed(db):
    """Resolving a public link full-scanned `keys`. That is invisible at fifty
    customers and is not at fifty thousand."""
    cur = await db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM keys WHERE sub_token = ?", ("x",))
    plan = " ".join(r[3] for r in await cur.fetchall())
    assert "idx_keys_sub_token" in plan
    assert "SCAN keys" not in plan


async def test_the_scheduler_sweeps_are_indexed(db):
    """Both run every pass and both looked at every key to find a handful."""
    cur = await db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM keys"
        " WHERE duration_days IS NOT NULL AND activated_ts IS NULL")
    assert "idx_keys_pending" in " ".join(r[3] for r in await cur.fetchall())
    cur = await db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM keys"
        " WHERE expiry_ts IS NOT NULL AND expiry_ts <= ? AND disabled = 0", (1,))
    plan = " ".join(r[3] for r in await cur.fetchall())
    assert "idx_keys_expiring" in plan
    assert "SCAN keys" not in plan


async def test_counting_keys_agrees_with_listing_them(db):
    await db.add_server("s1", "Tokyo", "https://x")
    assert await db.count_keys() == 0
    for n in range(3):
        await db.add_key("s1", str(n), f"k{n}", None, None)
    assert await db.count_keys() == len(await db.all_keys()) == 3


# ------------------------------------------------------------- the settings
async def test_a_settings_view_reads_the_table_once_and_agrees_with_the_store(app):
    """`knobs()` asked for twenty settings one row at a time to answer one
    request."""
    _, deps, _ = app
    reads = 0
    original = deps.db.get_setting

    async def counted(key):
        nonlocal reads
        reads += 1
        return await original(key)

    deps.db.get_setting = counted
    try:
        view = await deps.settings.view()
        knobs = view.knobs()
    finally:
        deps.db.get_setting = original

    assert reads == 0                                  # one all_settings, no row reads
    assert knobs == await deps.settings.knobs()        # and the same answers
    assert view.num("metrics_ttl") == await deps.settings.num("metrics_ttl")
    assert view.profile_base() == await deps.settings.get_profile_base()


async def test_a_view_honours_the_range_check_the_store_does(app):
    """A hand-edited interval of 0 would spin the scheduler flat out. The view
    is a second reader of the same table and must not be the lenient one."""
    _, deps, _ = app
    await deps.db.set_setting("expiry_check_interval", "0")
    assert (await deps.settings.view()).num("expiry_check_interval") == 60
    await deps.db.set_setting("expiry_check_interval", "not-a-number")
    assert (await deps.settings.view()).num("expiry_check_interval") == 60


async def test_listing_keys_does_not_re_read_settings_per_server(app):
    """Every server re-read the metrics TTL and the profile base for itself, so
    the settings cost of one page grew with the size of the fleet."""
    import httpx

    application, deps, fakes = app
    for sid, fake in fakes.items():
        key = await fake.create_key(name="Ali")
        await deps.db.add_key(sid, key["id"], "Ali", None, None)

    reads = 0
    original = deps.db.get_setting

    async def counted(key):
        nonlocal reads
        reads += 1
        return await original(key)

    transport = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=transport, base_url="http://x")
    r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 200, r.text

    deps.db.get_setting = counted
    try:
        r = await c.get("/api/keys")
    finally:
        deps.db.get_setting = original
    await c.aclose()

    assert r.status_code == 200
    assert len(r.json()["keys"]) == 5
    # Only the session-lifetime lookup in current_admin is left. Before, this
    # was that plus two row reads for each of the five servers.
    assert reads <= 2, f"{reads} settings row reads for one /api/keys"
