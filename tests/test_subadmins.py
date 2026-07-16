"""Sub-admin scoping and capabilities, over HTTP against the real app."""
import os
import sys
import tempfile

import httpx
import pytest

from test_features import FakeOutline  # a fake with limits/usage


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
    fakes = {}
    for sid, name in (("s1", "Tokyo"), ("s2", "Berlin")):
        f = FakeOutline()
        fakes[sid] = f
        deps.reg.servers[sid] = {"id": sid, "name": name,
                                 "api_url": "https://1.2.3.4:1/x",
                                 "cert_sha256": None, "api": f}
        await deps.db.add_server(sid, name, "https://1.2.3.4:1/x")
    yield appmod.app, deps, fakes
    await deps.db.close()


async def _login(application, username, password):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    r = await c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


async def _mk_sub(deps, username="sara", caps="keys.view", servers="s1"):
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    return await deps.db.add_admin(username, h, s, caps=caps, servers=servers)


# ------------------------------------------------------------------ identity
async def test_owner_logs_in_with_the_existing_password(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    me = (await c.get("/api/me")).json()
    assert me["username"] == "admin" and me["isOwner"] is True
    assert "keys.create" in me["caps"] and me["servers"] == []
    await c.aclose()


async def test_a_pre_identity_cookie_is_rejected(app):
    """Old sessions signed a bare random string. There is no honest way to map
    one to an admin, so they must fail closed rather than pass as anyone."""
    application, deps, _ = app
    from outline_panel.web import deps as d
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        c.cookies.set("outline_session", d.signer.dumps("deadbeef"))  # the old shape
        assert (await c.get("/api/me")).status_code == 401


async def test_disabling_an_admin_kills_the_live_session(app):
    application, deps, _ = app
    aid = await _mk_sub(deps)
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/me")).status_code == 200
    await deps.db.update_admin(aid, disabled=1)
    assert (await c.get("/api/me")).status_code == 401  # same cookie, now dead
    await c.aclose()


async def test_deleting_an_admin_kills_the_live_session(app):
    application, deps, _ = app
    aid = await _mk_sub(deps)
    c = await _login(application, "sara", "sara-pw")
    await deps.db.delete_admin(aid)
    assert (await c.get("/api/keys")).status_code == 401
    await c.aclose()


# --------------------------------------------------------------------- scope
async def test_out_of_scope_server_does_not_exist(app):
    """Scoped to s1: s2 is absent from every list and 404s on direct access."""
    application, deps, _ = app
    await _mk_sub(deps, caps="keys.view,keys.create,keys.edit,keys.delete", servers="s1")
    c = await _login(application, "sara", "sara-pw")

    servers = (await c.get("/api/servers")).json()["servers"]
    assert [s["id"] for s in servers] == ["s1"]

    assert (await c.get("/api/keys?server=s2")).status_code == 404
    assert (await c.get("/api/stats?server=s2")).status_code == 404
    assert (await c.get("/api/servers/s2/settings")).status_code == 404
    assert (await c.post("/api/servers/s2/keys",
                         json={"name": "X", "limit_gb": 0, "days": 0})).status_code == 404
    assert (await c.delete("/api/servers/s2/keys/1")).status_code == 404
    await c.aclose()


async def test_unfiltered_list_means_my_servers_only(app):
    """?server= omitted must mean "all of mine", not all of them."""
    application, deps, fakes = app
    owner = await _login(application, "admin", "pw")
    await owner.post("/api/servers/s1/keys", json={"name": "Tokyo1", "limit_gb": 0, "days": 0})
    await owner.post("/api/servers/s2/keys", json={"name": "Berlin1", "limit_gb": 0, "days": 0})
    assert len((await owner.get("/api/keys")).json()["keys"]) == 2

    await _mk_sub(deps, caps="keys.view", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    names = [k["name"] for k in (await c.get("/api/keys")).json()["keys"]]
    assert names == ["Tokyo1"]
    await owner.aclose()
    await c.aclose()


async def test_sub_mirror_cannot_reach_an_out_of_scope_server(app):
    """This route takes {target}, not {sid}, so the router guard never sees it —
    without its own check it would mint a key on any server in the panel."""
    application, deps, fakes = app
    owner = await _login(application, "admin", "pw")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "A", "limit_gb": 0, "days": 0})).json()["id"]
    token = (await owner.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]

    await _mk_sub(deps, caps="keys.view,keys.edit", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.post(f"/api/sub/{token}/servers/s2")).status_code == 404
    assert len(fakes["s2"].keys) == 0  # nothing was created over there
    assert (await c.delete(f"/api/sub/{token}/servers/s2")).status_code == 404

    # and the mirror-onto list only offers servers she can see
    info = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()
    assert [s["id"] for s in info["servers"]] == ["s1"]
    await owner.aclose()
    await c.aclose()


# -------------------------------------------------------------- capabilities
async def test_each_capability_is_enforced(app):
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "A", "limit_gb": 0, "days": 0})).json()["id"]

    await _mk_sub(deps, caps="keys.view", servers="s1")  # view only
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/keys")).status_code == 200
    assert (await c.get("/api/stats")).status_code == 200
    assert (await c.post("/api/servers/s1/keys",
                         json={"name": "B", "limit_gb": 0, "days": 0})).status_code == 403
    assert (await c.put(f"/api/servers/s1/keys/{kid}/name",
                        json={"name": "B"})).status_code == 403
    assert (await c.delete(f"/api/servers/s1/keys/{kid}")).status_code == 403
    assert (await c.post("/api/servers",
                         json={"name": "N", "apiUrl": "https://1.2.3.4:1/x"})).status_code == 403
    await owner.aclose()
    await c.aclose()


async def test_view_without_create_still_reads(app):
    application, deps, _ = app
    await _mk_sub(deps, caps="keys.create,keys.view", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    r = await c.post("/api/servers/s1/keys", json={"name": "B", "limit_gb": 0, "days": 0})
    assert r.status_code == 200
    await c.aclose()


async def test_no_caps_at_all_can_see_nothing(app):
    application, deps, _ = app
    await _mk_sub(deps, caps="", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/me")).status_code == 200      # is logged in
    assert (await c.get("/api/keys")).status_code == 403    # but may do nothing
    await c.aclose()


# --------------------------------------------------------------- owner-only
async def test_owner_only_surfaces_are_closed_to_sub_admins(app):
    """Each of these is full access in disguise: the backup carries every
    password hash and the bot token, restore rewrites the panel, and the
    security settings are the owner's own credentials."""
    application, deps, _ = app
    await _mk_sub(deps, caps="keys.view,keys.create,keys.edit,keys.delete,servers.manage",
                  servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/backup")).status_code == 403
    assert (await c.post("/api/restore",
                         json={"servers": [], "keys": [], "settings": {},
                               "admins": []})).status_code == 403
    assert (await c.get("/api/settings")).status_code == 403
    assert (await c.post("/api/settings/password",
                         json={"current": "sara-pw", "new": "hacked1"})).status_code == 403
    assert (await c.post("/api/settings/2fa/start")).status_code == 403
    await c.aclose()


async def test_bot_is_delegatable(app):
    application, deps, _ = app
    await _mk_sub(deps, caps="keys.view", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/settings/bot")).status_code == 403
    await c.aclose()

    await _mk_sub(deps, username="bo", caps="keys.view,bot.manage", servers="s1")
    c2 = await _login(application, "bo", "sara-pw")
    assert (await c2.get("/api/settings/bot")).status_code == 200
    await c2.aclose()


# ------------------------------------------------------------- admin CRUD
async def test_owner_creates_and_scopes_an_admin(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    r = await c.post("/api/admins", json={"username": "sara", "password": "sara-pw",
                                          "caps": ["keys.view", "keys.create"],
                                          "servers": ["s1"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "sara" and body["isOwner"] is False
    assert body["caps"] == ["keys.view", "keys.create"] and body["servers"] == ["s1"]
    assert "pw_hash" not in body and "pw_salt" not in body  # never leaves the server

    # and the password she was given actually works
    sara = await _login(application, "sara", "sara-pw")
    assert (await sara.get("/api/keys")).status_code == 200
    await sara.aclose()
    await c.aclose()


async def test_admin_crud_rejects_nonsense(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    base = {"username": "x1", "password": "sara-pw", "servers": ["s1"]}
    assert (await c.post("/api/admins", json={**base, "caps": ["keys.everything"]})
            ).status_code == 400          # unknown capability
    assert (await c.post("/api/admins", json={**base, "servers": ["nope"]})
            ).status_code == 400          # unknown server
    assert (await c.post("/api/admins", json={**base, "servers": []})
            ).status_code == 400          # empty = every server, must be explicit
    assert (await c.post("/api/admins", json={**base, "password": "short"})
            ).status_code == 422
    await c.post("/api/admins", json=base)
    assert (await c.post("/api/admins", json=base)).status_code == 400  # dupe username
    await c.aclose()


async def test_owner_row_is_protected(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    owner_id = (await deps.db.get_owner())["id"]
    assert (await c.put(f"/api/admins/{owner_id}",
                        json={"disabled": True})).status_code == 400
    assert (await c.put(f"/api/admins/{owner_id}",
                        json={"servers": ["s1"]})).status_code == 400
    assert (await c.delete(f"/api/admins/{owner_id}")).status_code == 400
    assert (await deps.db.get_owner())["disabled"] == 0
    await c.aclose()


async def test_sub_admin_cannot_reach_admin_management(app):
    """The escalation that matters: create an admin and you can grant yourself
    anything, so this must be closed to everyone but the owner."""
    application, deps, _ = app
    await _mk_sub(deps, caps="keys.view,keys.create,keys.edit,keys.delete,"
                             "servers.manage,bot.manage", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/admins")).status_code == 403
    assert (await c.post("/api/admins", json={"username": "evil", "password": "evil-pw",
                                              "caps": [], "servers": ["s1"]})
            ).status_code == 403
    assert (await c.delete("/api/admins/1")).status_code == 403
    await c.aclose()


async def test_editing_an_admin_takes_effect_immediately(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    aid = (await c.post("/api/admins", json={"username": "sara", "password": "sara-pw",
                                             "caps": ["keys.view"], "servers": ["s1"]})
           ).json()["id"]
    sara = await _login(application, "sara", "sara-pw")
    assert (await sara.post("/api/servers/s1/keys",
                            json={"name": "A", "limit_gb": 0, "days": 0})).status_code == 403

    await c.put(f"/api/admins/{aid}", json={"caps": ["keys.view", "keys.create"]})
    assert (await sara.post("/api/servers/s1/keys",
                            json={"name": "A", "limit_gb": 0, "days": 0})).status_code == 200
    await sara.aclose()
    await c.aclose()
