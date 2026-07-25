"""
The audit trail. `credit_ledger` says where the money went; this says who did
everything else.
"""
import json
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


async def _login(application, username="admin", password="pw"):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    r = await c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


async def _actions(deps):
    return [e["action"] for e in await deps.db.audit_page(limit=500)]


async def test_a_mutation_is_recorded_with_who_what_and_the_outcome(app):
    application, deps, _ = app
    c = await _login(application)
    r = await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 5})
    assert r.status_code == 200
    kid = r.json()["id"]

    entry = (await deps.db.audit_page(limit=1))[0]
    assert entry["action"] == "POST /api/servers/{sid}/keys"
    assert entry["target"] == f"s1/{kid}" or entry["target"] == "s1"
    assert entry["status"] == 200
    assert entry["actor_name"] == "admin"
    assert json.loads(entry["detail"])["name"] == "Ali"
    await c.aclose()


async def test_reads_are_not_recorded(app):
    """The dashboard polls /api/keys and /api/stats every few seconds; logging
    those would bury every real action."""
    application, deps, _ = app
    c = await _login(application)
    for _ in range(5):
        await c.get("/api/keys")
        await c.get("/api/stats")
        await c.get("/api/servers")
    assert not any(a.startswith("GET") for a in await _actions(deps))
    await c.aclose()


async def test_secrets_never_reach_the_log(app):
    """The log is read by whoever holds the panel; it must not become a second
    copy of every password and token in it."""
    application, deps, _ = app
    c = await _login(application)
    await c.post("/api/admins", json={"username": "sara", "password": "hunter2000",
                                      "caps": ["keys.view"], "servers": ["s1"]})
    await c.post("/api/settings/password",
                 json={"current": "pw", "new": "brand-new-secret"})
    rows = await deps.db.audit_page(limit=50)
    blob = json.dumps(rows)
    assert "hunter2000" not in blob
    assert "brand-new-secret" not in blob
    # the field is kept, so the log still shows a password *was* set — only the
    # value is gone (detail is JSON nested inside a JSON column, hence the parse)
    details = [json.loads(r["detail"]) for r in rows if r["detail"]]
    assert {"username": "sara", "password": "***", "caps": ["keys.view"],
            "servers": ["s1"]} in details
    assert {"current": "***", "new": "***"} in details
    await c.aclose()


async def test_a_failed_login_is_recorded_without_an_actor(app):
    """A run of these is what a break-in attempt looks like, so they are the
    entries most worth having — with the password redacted."""
    application, deps, _ = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await c.post("/api/login", json={"username": "admin", "password": "wrong"})
    entry = (await deps.db.audit_page(limit=1))[0]
    assert entry["action"] == "POST /api/login"
    assert entry["status"] == 401
    assert entry["actor_admin_id"] is None and entry["actor_name"] is None
    assert "wrong" not in (entry["detail"] or "")


async def test_a_refused_action_is_recorded_too(app):
    """'Sara tried to reach the admin list' is exactly what you want to see."""
    application, deps, _ = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    r = await c.post("/api/admins", json={"username": "x", "password": "yyyyyy",
                                          "caps": [], "servers": ["s1"]})
    assert r.status_code == 403
    entry = (await deps.db.audit_page(limit=1))[0]
    assert entry["status"] == 403 and entry["actor_name"] == "sara"
    await c.aclose()


async def test_a_restore_is_recorded_but_its_payload_is_not(app):
    """The payload is the whole database, hashes included."""
    application, deps, _ = app
    c = await _login(application)
    await c.post("/api/restore", json={"servers": [], "keys": [], "admins": [],
                                       "settings": {"admin_password_hash": "deadbeef"},
                                       "packages": [], "ledger": []})
    entry = next(e for e in await deps.db.audit_page(limit=20)
                 if e["action"].endswith("/api/restore"))
    assert entry["detail"] is None
    assert "deadbeef" not in json.dumps(await deps.db.audit_page(limit=20))
    await c.aclose()


async def test_the_log_survives_a_restore(app):
    """Wiping it during a restore would erase the record of the restore."""
    application, deps, _ = app
    c = await _login(application)
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 1})
    before = len(await deps.db.audit_page(limit=500))
    await c.post("/api/restore", json={"servers": [], "keys": [], "admins": [],
                                       "settings": {}, "packages": [], "ledger": []})
    assert len(await deps.db.audit_page(limit=500)) > before
    await c.aclose()


async def test_the_reader_is_owner_only_and_pages(app):
    application, deps, _ = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view", servers="s1")

    c = await _login(application)
    for i in range(7):
        await c.post("/api/servers/s1/keys", json={"name": f"u{i}", "limit_gb": 1})
    r = await c.get("/api/audit?limit=3")
    body = r.json()
    assert len(body["entries"]) == 3 and body["nextBeforeId"] is not None
    nxt = (await c.get(f"/api/audit?limit=3&before_id={body['nextBeforeId']}")).json()
    ids = [e["id"] for e in body["entries"]] + [e["id"] for e in nxt["entries"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 6
    await c.aclose()

    sub = await _login(application, "sara", "sara-pw")
    assert (await sub.get("/api/audit")).status_code == 403
    await sub.aclose()


async def test_a_broken_audit_write_does_not_break_the_request(app, monkeypatch):
    """Bookkeeping must never turn a served request into a 500."""
    application, deps, _ = app
    c = await _login(application)

    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(deps.db, "add_audit", boom)
    r = await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 1})
    assert r.status_code == 200 and r.json()["name"] == "Ali"
    await c.aclose()


async def test_the_scheduler_prunes_old_entries(app, monkeypatch):
    application, deps, _ = app
    import outline_panel.core.db as dbmod
    from outline_panel.core import scheduler
    monkeypatch.setattr(dbmod.time, "time", lambda: 1000)
    await deps.db.add_audit(1, "admin", "ancient", None, 200, None, None)
    monkeypatch.undo()
    await deps.db.add_audit(1, "admin", "recent", None, 200, None, None)

    await deps.settings.set("audit_retention_days", "1")
    await scheduler._check_once(deps.reg, deps.db, None, set(), deps.settings)
    assert [e["action"] for e in await deps.db.audit_page()] == ["recent"]
