"""
The other half of converging: actually retrying, and noticing drift.

The executor queues what could not reach a server. Without something draining
that queue, "converged" is a nicer word for "lost" — so these cover the drain,
its backoff, and the reconciliation report an operator reads before switching
reconciliation on.
"""

import os
import sys
import tempfile
import time

import httpx
import pytest

from test_features import FakeOutline

GB = 1024 ** 3


@pytest.fixture
async def two_servers():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "conv.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.dirname(__file__))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    fakes = {}
    for sid, name in (("s1", "Tokyo"), ("s2", "Berlin")):
        f = FakeOutline()
        fakes[sid] = f
        deps.reg.servers[sid] = {"id": sid, "name": name,
                                 "api_url": f"https://10.0.0.1:1/{sid}",
                                 "cert_sha256": None, "api": f}
        await deps.db.add_server(sid, name, f"https://10.0.0.1:1/{sid}")
    yield appmod.app, deps, fakes
    await deps.db.close()


async def _mirrored(two_servers):
    application, deps, fakes = two_servers
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                          base_url="http://panel.example.com")
    await c.post("/api/login", json={"password": "pw"})
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "cust", "limit_gb": 40, "days": 30,
                              "start_now": True, "extra_servers": ["s2"]})).json()["id"]
    token = (await deps.db.get_key("s1", kid))["sub_token"]
    return c, kid, token


# ------------------------------------------------------------------- drain
async def test_a_deferred_suspension_lands_when_the_server_comes_back(two_servers):
    """The promise the outbox makes. Suspend while a server is down, bring it
    back, and the customer is cut off there too — without anyone re-doing it."""
    from outline_panel.application import convergence
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    fakes["s2"].fail_limit_writes = True
    await c.post(f"/api/servers/s1/keys/{kid}/disable")
    assert len(await deps.db.pending_commands(token)) == 1

    fakes["s2"].fail_limit_writes = False
    # the row was scheduled into the future, so "now" has to reach it
    summary = await convergence.drain(deps.db, deps.reg, int(time.time()) + 60)

    assert summary == {"applied": 1, "failed": 0, "dropped": 0}
    assert await deps.db.pending_commands(token) == []
    mirror = [m for m in await deps.db.get_keys_by_sub_token(token)
              if m["server_id"] == "s2"][0]
    assert fakes["s2"].limits[mirror["key_id"]] == 0
    await c.aclose()


async def test_a_still_failing_effect_backs_off_rather_than_spinning(two_servers):
    """A dead server must not be hammered once per scheduler pass forever."""
    from outline_panel.application import convergence
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    fakes["s2"].fail_limit_writes = True
    await c.post(f"/api/servers/s1/keys/{kid}/disable")

    first = (await deps.db.pending_commands(token))[0]
    summary = await convergence.drain(deps.db, deps.reg, int(time.time()) + 60)
    assert summary["failed"] == 1
    again = (await deps.db.pending_commands(token))[0]
    assert again["attempts"] == first["attempts"] + 1
    assert again["next_try_ts"] > first["next_try_ts"], "the retry did not back off"
    assert again["last_error"]
    await c.aclose()


async def test_an_effect_for_a_removed_server_is_dropped(two_servers):
    """Nothing left to converge with: the server is not on this panel any more,
    so retrying it forever is the only wrong answer."""
    from outline_panel.application import convergence
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    fakes["s2"].fail_limit_writes = True
    await c.post(f"/api/servers/s1/keys/{kid}/disable")

    await deps.reg.remove("s2")
    summary = await convergence.drain(deps.db, deps.reg, int(time.time()) + 60)
    assert summary["dropped"] == 1
    assert await deps.db.outbox_depth() == 0
    await c.aclose()


async def test_backoff_is_bounded():
    from outline_panel.application.convergence import backoff_for
    delays = [backoff_for(n) for n in range(1, 12)]
    assert delays == sorted(delays), "backoff must never shorten"
    assert delays[-1] <= 3600, "an hour is long enough to wait for a dead server"
    assert delays[0] <= 60, "the first retry should be prompt — most failures are blips"


# ----------------------------------------------------------------- drift
async def test_drift_reports_a_customer_who_is_suspended_here_but_live_there(two_servers):
    """The report an operator reads before switching reconciliation on. This is
    exactly the shape of what the A1 defect left behind."""
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    mirror = [m for m in await deps.db.get_keys_by_sub_token(token)
              if m["server_id"] == "s2"][0]
    # the panel believes it is suspended; the server never got the message
    await deps.db.set_disabled("s2", mirror["key_id"], True)

    r = await c.get("/api/convergence/drift")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False, "reconciliation must be opt-in"
    assert body["wouldSuspend"] == 1
    hit = [d for d in body["differences"] if d["serverId"] == "s2"][0]
    assert hit["issue"] == "limit" and hit["suspended"] is True
    assert hit["want"] == 0 and hit["have"] == 40 * GB

    # ...and looking must not have changed anything
    assert await deps.db.outbox_depth() == 0
    await c.aclose()


async def test_applying_the_drift_queues_the_suspension(two_servers):
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    mirror = [m for m in await deps.db.get_keys_by_sub_token(token)
              if m["server_id"] == "s2"][0]
    await deps.db.set_disabled("s2", mirror["key_id"], True)

    assert (await c.post("/api/convergence/drift")).json()["queued"] == 1
    from outline_panel.application import convergence
    await convergence.drain(deps.db, deps.reg, int(time.time()) + 60)
    assert fakes["s2"].limits[mirror["key_id"]] == 0
    await c.aclose()


async def test_a_key_made_in_outline_manager_is_never_touched(two_servers):
    """Adoption is ensure_local's job. A reconciler that deleted unknown
    upstream keys would be a data-loss bug that runs on a timer."""
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    stray = await fakes["s1"].create_key(name="made-by-hand")

    r = await c.post("/api/convergence/drift")
    assert r.json()["queued"] == 0
    assert stray["id"] in fakes["s1"].keys, "the reconciler deleted a real key"
    await c.aclose()


async def test_drift_reports_a_key_deleted_upstream_without_recreating_it(two_servers):
    """Someone removed it in Outline Manager. Putting it back would undo a
    deliberate act, so it is reported and left alone."""
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    fakes["s1"].keys.pop(kid)

    body = (await c.get("/api/convergence/drift")).json()
    gone = [d for d in body["differences"] if d["issue"] == "missing"]
    assert [d["keyId"] for d in gone] == [kid]
    assert body["wouldSuspend"] == 0
    await c.aclose()


async def test_the_report_is_owner_only(two_servers):
    """It names every customer on every server — the panel's whole book."""
    application, deps, fakes = two_servers
    c, kid, token = await _mirrored(two_servers)
    await c.post("/api/admins", json={"username": "sara", "password": "sara-pw",
                                      "caps": ["keys.view"], "servers": ["s1"]})
    sara = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://panel.example.com")
    await sara.post("/api/login", json={"username": "sara", "password": "sara-pw"})
    assert (await sara.get("/api/convergence/drift")).status_code == 403
    assert (await sara.get("/api/convergence")).status_code == 403
    assert (await sara.post("/api/convergence/drift")).status_code == 403
    await sara.aclose()
    await c.aclose()
