"""
Server health history. Reachability used to be computed per request and thrown
away, so "has this been flapping all week?" had no answer.
"""
import os
import sys
import tempfile

import httpx
import pytest

from outline_panel.core import scheduler
from outline_panel.core.db import DB
from outline_panel.core.outline_api import OutlineError
from outline_panel.core.settings import SettingsStore


class Server:
    def __init__(self, alive=True):
        self.alive = alive

    async def get_server_info(self):
        if not self.alive:
            raise OutlineError("connection refused")
        return {"name": "Tokyo", "version": "1.0"}


class Reg:
    def __init__(self, servers):
        self.servers = servers

    def ids(self):
        return list(self.servers)

    def get(self, sid):
        return self.servers.get(sid)

    def meta(self, sid):
        return {"id": sid, "name": f"srv-{sid}"} if sid in self.servers else None


@pytest.fixture
async def db():
    d = DB(os.path.join(tempfile.mkdtemp(), "h.db"))
    await d.init()
    yield d
    await d.close()


async def _pass(reg, db, notifier=None, notified=None, settings=None):
    await scheduler._probe_servers(reg, db, notifier, notified if notified is not None
                                   else set(), settings or SettingsStore(db))


async def test_a_probe_is_recorded_both_ways(db):
    reg = Reg({"s1": Server(True), "s2": Server(False)})
    await _pass(reg, db)
    assert [h["reachable"] for h in await db.health_history("s1")] == [1]
    down = (await db.health_history("s2"))[0]
    assert down["reachable"] == 0 and "refused" in down["error"]
    assert down["latency_ms"] is None      # no latency for a call that failed
    assert (await db.health_history("s1"))[0]["latency_ms"] is not None


async def test_uptime_is_summarised(db):
    srv = Server(True)
    reg = Reg({"s1": srv})
    for alive in (True, True, False, True):
        srv.alive = alive
        await _pass(reg, db)
    summary = await db.health_summary(0)
    assert summary["s1"]["probes"] == 4 and summary["s1"]["ok"] == 3
    assert summary["s1"]["uptimePct"] == 75.0
    assert summary["s1"]["avgLatencyMs"] is not None


async def test_consecutive_failures_counts_only_the_current_run(db):
    srv = Server(True)
    reg = Reg({"s1": srv})
    for alive in (False, False, True, False, False, False):
        srv.alive = alive
        await _pass(reg, db)
    assert await db.consecutive_failures("s1") == 3      # not 5
    srv.alive = True
    await _pass(reg, db)
    assert await db.consecutive_failures("s1") == 0


async def test_one_blip_does_not_alert(db):
    """Paging on a single missed check is how people learn to ignore alerts."""
    srv = Server(True)
    reg = Reg({"s1": srv})
    msgs, notified = [], set()

    async def notifier(text, **kw):
        msgs.append(text)

    srv.alive = False
    await _pass(reg, db, notifier, notified)
    assert msgs == []


async def test_sustained_downtime_alerts_once_then_recovers(db):
    srv = Server(True)
    reg = Reg({"s1": srv})
    msgs, notified = [], set()

    async def notifier(text, **kw):
        msgs.append(text)

    srv.alive = False
    for _ in range(5):                      # threshold is 3
        await _pass(reg, db, notifier, notified)
    assert len([m for m in msgs if "looks down" in m]) == 1, "alerted more than once"

    srv.alive = True
    await _pass(reg, db, notifier, notified)
    assert any("reachable again" in m for m in msgs)

    # and it can alert again on the next outage
    srv.alive = False
    for _ in range(4):
        await _pass(reg, db, notifier, notified)
    assert len([m for m in msgs if "looks down" in m]) == 2


async def test_the_threshold_is_configurable(db):
    srv = Server(False)
    reg = Reg({"s1": srv})
    s = SettingsStore(db)
    await s.set("health_alert_failures", "1")
    msgs, notified = [], set()

    async def notifier(text, **kw):
        msgs.append(text)

    await _pass(reg, db, notifier, notified, s)
    assert len(msgs) == 1


async def test_history_is_pruned(db):
    import outline_panel.core.db as dbmod
    reg = Reg({"s1": Server(True)})
    real = dbmod.time.time
    dbmod.time.time = lambda: 1000
    try:
        await _pass(reg, db)
    finally:
        dbmod.time.time = real
    await _pass(reg, db)
    assert len(await db.health_history("s1")) == 2
    await db.prune_health(5000)
    assert len(await db.health_history("s1")) == 1


# ------------------------------------------------------------------ the API
@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.dirname(__file__))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    from test_features import FakeOutline
    await deps.db.init()
    await deps.settings.bootstrap()
    for sid, name in (("s1", "Tokyo"), ("s2", "Berlin")):
        deps.reg.servers[sid] = {"id": sid, "name": name,
                                 "api_url": "https://1.2.3.4:1/x",
                                 "cert_sha256": None, "api": FakeOutline()}
        await deps.db.add_server(sid, name, "https://1.2.3.4:1/x")
    yield appmod.app, deps
    await deps.db.close()


async def _login(application, user="admin", pw="pw"):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    assert (await c.post("/api/login", json={"username": user, "password": pw})).status_code == 200
    return c


async def test_the_health_api_reports_uptime(app):
    application, deps = app
    for ok in (True, True, False):
        await deps.db.record_health("s1", ok, 12 if ok else None, None if ok else "boom")
    c = await _login(application)
    body = (await c.get("/api/servers/health")).json()
    s1 = next(s for s in body["servers"] if s["id"] == "s1")
    assert s1["probes"] == 3 and s1["uptimePct"] == 66.67
    assert s1["failingNow"] == 1
    hist = (await c.get("/api/servers/s1/health")).json()["history"]
    assert len(hist) == 3 and hist[0]["reachable"] == 0
    await c.aclose()


async def test_a_sub_admin_only_sees_their_own_servers(app):
    """Same scoping as everything else: an out-of-scope server does not exist."""
    application, deps = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view", servers="s1")
    await deps.db.record_health("s1", True, 10, None)
    await deps.db.record_health("s2", True, 10, None)
    c = await _login(application, "sara", "sara-pw")
    ids = [s["id"] for s in (await c.get("/api/servers/health")).json()["servers"]]
    assert ids == ["s1"]
    assert (await c.get("/api/servers/s2/health")).status_code == 404
    await c.aclose()
