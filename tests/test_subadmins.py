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
    k1 = (await owner.post("/api/servers/s1/keys",
                           json={"name": "Tokyo1", "limit_gb": 0, "days": 0})).json()["id"]
    await owner.post("/api/servers/s2/keys", json={"name": "Berlin1", "limit_gb": 0, "days": 0})
    assert len((await owner.get("/api/keys")).json()["keys"]) == 2

    aid = await _mk_sub(deps, caps="keys.view", servers="s1")
    await deps.db.set_key_owner("s1", k1, aid)   # hers now
    c = await _login(application, "sara", "sara-pw")
    names = [k["name"] for k in (await c.get("/api/keys")).json()["keys"]]
    assert names == ["Tokyo1"]                   # not Berlin1: not her server
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

    aid = await _mk_sub(deps, caps="keys.view,keys.edit", servers="s1")
    await deps.db.set_key_owner("s1", kid, aid)   # the sub is hers to mirror
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

    aid = await _mk_sub(deps, caps="keys.view", servers="s1")  # view only
    await deps.db.set_key_owner("s1", kid, aid)                # and it is hers
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


# ------------------------------------------------------- credit & packages
async def _credit_sub(deps, credit=100_000, discount=0, caps="keys.view,keys.create,keys.edit"):
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    aid = await deps.db.add_admin("cara", h, s, caps=caps, servers="s1")
    await deps.db.conn.execute(
        "UPDATE admins SET credit_enabled = 1, discount_pct = ? WHERE id = ?",
        (discount, aid))
    await deps.db.conn.commit()
    if credit:
        await deps.db.credit_admin(aid, credit, reason="topup")
    return aid


async def test_credit_admin_buys_a_package(app):
    application, deps, fakes = app
    aid = await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")

    r = await c.post("/api/servers/s1/keys",
                     json={"name": "U1", "package_id": pid})
    assert r.status_code == 200, r.text
    assert r.json()["limit"] == 5 * 1024 ** 3        # the package decided, not her
    assert r.json()["durationDays"] == 30
    assert (await deps.db.get_admin(aid))["credit"] == 70_000
    row = (await deps.db.ledger_for(aid))[0]
    assert row["reason"] == "purchase" and row["delta"] == -30_000
    assert row["key_id"] == r.json()["id"]
    await c.aclose()


async def test_the_package_overrides_what_the_admin_asks_for(app):
    """Otherwise the picker is decoration and she sells herself 500 GB."""
    application, deps, fakes = app
    await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")
    r = await c.post("/api/servers/s1/keys",
                     json={"name": "U1", "package_id": pid,
                           "limit_gb": 500, "days": 3650, "monthly_gb": 999})
    assert r.json()["limit"] == 5 * 1024 ** 3
    assert r.json()["durationDays"] == 30
    await c.aclose()


async def test_credit_admin_must_pick_a_package(app):
    application, deps, _ = app
    await _credit_sub(deps, credit=100_000)
    c = await _login(application, "cara", "sara-pw")
    r = await c.post("/api/servers/s1/keys", json={"name": "U1", "limit_gb": 5, "days": 30})
    assert r.status_code == 400 and "package" in r.json()["detail"].lower()
    await c.aclose()


async def test_zero_credit_cannot_sell(app):
    application, deps, _ = app
    aid = await _credit_sub(deps, credit=30_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")
    assert (await c.post("/api/servers/s1/keys",
                         json={"name": "U1", "package_id": pid})).status_code == 200
    r = await c.post("/api/servers/s1/keys", json={"name": "U2", "package_id": pid})
    assert r.status_code == 402 and "credit" in r.json()["detail"].lower()
    assert (await deps.db.get_admin(aid))["credit"] == 0
    await c.aclose()


async def test_discount_applies(app):
    application, deps, _ = app
    aid = await _credit_sub(deps, credit=100_000, discount=25)
    pid = await deps.db.add_package("Odd price", 5, 30, 33_333)
    c = await _login(application, "cara", "sara-pw")

    pkgs = (await c.get("/api/packages")).json()
    assert pkgs["packages"][0]["price"] == 25_000       # round(33333*0.75)
    assert pkgs["packages"][0]["basePrice"] == 33_333
    assert pkgs["packages"][0]["affordable"] is True

    await c.post("/api/servers/s1/keys", json={"name": "U1", "package_id": pid})
    assert (await deps.db.get_admin(aid))["credit"] == 75_000
    row = (await deps.db.ledger_for(aid))[0]
    assert row["delta"] == -25_000 and row["price_before_discount"] == 33_333
    await c.aclose()


async def test_a_failed_sale_gives_the_credit_back(app):
    """Nothing was bought, so keeping the money would just be a bug."""
    application, deps, fakes = app
    aid = await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")

    async def boom(name=None, limit_bytes=None):
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("server down")
    fakes["s1"].create_key = boom

    r = await c.post("/api/servers/s1/keys", json={"name": "U1", "package_id": pid})
    assert r.status_code == 502
    assert (await deps.db.get_admin(aid))["credit"] == 100_000   # made whole
    reasons = [x["reason"] for x in await deps.db.ledger_for(aid)]
    assert reasons == ["reversal", "purchase", "topup"]          # and it is visible
    assert await deps.db.ledger_sum(aid) == 100_000
    await c.aclose()


async def test_renewing_costs_another_package(app):
    application, deps, _ = app
    aid = await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "U1", "package_id": pid})).json()["id"]
    assert (await deps.db.get_admin(aid))["credit"] == 70_000

    r = await c.post(f"/api/servers/s1/keys/{kid}/extend", json={"package_id": pid})
    assert r.status_code == 200
    assert (await deps.db.get_admin(aid))["credit"] == 40_000     # charged again
    meta = await deps.db.get_key("s1", kid)
    assert meta["limit_bytes"] == 10 * 1024 ** 3                  # 5 + 5 added
    assert meta["duration_days"] == 60                            # still pending: 30 + 30
    await c.aclose()


async def test_credit_admin_cannot_extend_for_free(app):
    """Free-form days would be a way around the price list entirely."""
    application, deps, _ = app
    await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "U1", "package_id": pid})).json()["id"]
    r = await c.post(f"/api/servers/s1/keys/{kid}/extend", json={"days": 3650})
    assert r.status_code == 400 and "package" in r.json()["detail"].lower()
    await c.aclose()


async def test_owner_and_exempt_admins_are_untouched(app):
    """The credit system is opt-in; everyone else keeps the free-form form."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    r = await owner.post("/api/servers/s1/keys",
                         json={"name": "O1", "limit_gb": 7, "days": 5})
    assert r.status_code == 200 and r.json()["limit"] == 7 * 1024 ** 3

    await _mk_sub(deps, username="free", caps="keys.create", servers="s1")
    c = await _login(application, "free", "sara-pw")
    r = await c.post("/api/servers/s1/keys", json={"name": "F1", "limit_gb": 9, "days": 5})
    assert r.status_code == 200 and r.json()["limit"] == 9 * 1024 ** 3
    await owner.aclose()
    await c.aclose()


async def test_packages_are_owner_only_to_edit(app):
    application, deps, _ = app
    await _credit_sub(deps, credit=1)
    c = await _login(application, "cara", "sara-pw")
    assert (await c.get("/api/packages")).status_code == 200      # may read the list
    assert (await c.post("/api/packages",
                         json={"name": "Free", "gb": 999, "days": 999,
                               "price": 0})).status_code == 403
    assert (await c.put("/api/packages/1",
                        json={"name": "x", "price": 0})).status_code == 403
    assert (await c.delete("/api/packages/1")).status_code == 403
    await c.aclose()


async def test_owner_tops_up_and_reads_the_statement(app):
    application, deps, _ = app
    aid = await _credit_sub(deps, credit=0)
    c = await _login(application, "admin", "pw")

    r = await c.post(f"/api/admins/{aid}/credit",
                     json={"delta": 500_000, "note": "cash"})
    assert r.status_code == 200 and r.json()["credit"] == 500_000
    r = await c.post(f"/api/admins/{aid}/credit", json={"delta": -100_000, "note": "fix"})
    assert r.json()["credit"] == 400_000
    # a correction may not push a balance below zero
    assert (await c.post(f"/api/admins/{aid}/credit",
                         json={"delta": -999_999})).status_code == 400
    assert (await c.post(f"/api/admins/{aid}/credit", json={"delta": 0})).status_code == 400

    entries = (await c.get(f"/api/admins/{aid}/ledger")).json()["entries"]
    assert [e["reason"] for e in entries] == ["adjust", "topup"]
    assert entries[0]["balance_after"] == 400_000
    await c.aclose()


async def test_an_admin_can_read_their_own_statement(app):
    application, deps, _ = app
    await _credit_sub(deps, credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _login(application, "cara", "sara-pw")
    await c.post("/api/servers/s1/keys", json={"name": "U1", "package_id": pid})

    me = (await c.get("/api/me")).json()
    assert me["creditEnabled"] is True and me["credit"] == 70_000

    entries = (await c.get("/api/me/ledger")).json()["entries"]
    assert entries[0]["reason"] == "purchase" and entries[0]["delta"] == -30_000
    assert entries[0]["key_id"] is not None      # which user the money bought
    # but someone else's statement is not hers to read
    assert (await c.get("/api/admins/1/ledger")).status_code == 403
    await c.aclose()


async def test_credit_cannot_be_set_directly_through_the_editor(app):
    """Money moves only through the ledger; an editor field that writes the
    column would leave a balance nobody can account for."""
    application, deps, _ = app
    aid = await _credit_sub(deps, credit=50_000)
    c = await _login(application, "admin", "pw")
    r = await c.put(f"/api/admins/{aid}", json={"servers": ["s1"], "caps": ["keys.view"],
                                                "credit": 999_999})
    assert r.status_code == 200
    assert (await deps.db.get_admin(aid))["credit"] == 50_000     # ignored
    assert await deps.db.ledger_sum(aid) == 50_000
    await c.aclose()


# ------------------------------------------------------------- key ownership
async def test_a_sub_admin_sees_only_their_own_users(app):
    """Two resellers on one server must not see each other's customers."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    mine = (await owner.post("/api/servers/s1/keys",
                             json={"name": "Owner's", "limit_gb": 0, "days": 0})).json()["id"]
    a_id = await _mk_sub(deps, username="a", caps="keys.view,keys.create", servers="s1")
    b_id = await _mk_sub(deps, username="b", caps="keys.view,keys.create", servers="s1")

    ca = await _login(application, "a", "sara-pw")
    cb = await _login(application, "b", "sara-pw")
    ka = (await ca.post("/api/servers/s1/keys",
                        json={"name": "A's customer", "limit_gb": 0, "days": 0})).json()["id"]
    await cb.post("/api/servers/s1/keys", json={"name": "B's customer", "limit_gb": 0, "days": 0})

    assert [k["name"] for k in (await ca.get("/api/keys")).json()["keys"]] == ["A's customer"]
    assert [k["name"] for k in (await cb.get("/api/keys")).json()["keys"]] == ["B's customer"]
    # the owner sees all three, each labelled with who it belongs to
    allk = (await owner.get("/api/keys")).json()["keys"]
    assert sorted((k["name"], k["ownerName"]) for k in allk) == sorted(
        [("A's customer", "a"), ("B's customer", "b"), ("Owner's", "admin")])
    assert next(k for k in allk if k["id"] == ka)["ownerAdminId"] == a_id
    assert next(k for k in allk if k["id"] == mine)["ownerAdminId"] is None
    assert b_id
    await owner.aclose()
    await ca.aclose()
    await cb.aclose()


async def test_another_admins_user_does_not_exist_for_you(app):
    """404, not 403: a reseller should not even learn the key is there."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "Owner's", "limit_gb": 0, "days": 0})).json()["id"]
    await _mk_sub(deps, username="a", caps="keys.view,keys.edit,keys.delete", servers="s1")
    c = await _login(application, "a", "sara-pw")

    assert (await c.put(f"/api/servers/s1/keys/{kid}/name",
                        json={"name": "stolen"})).status_code == 404
    assert (await c.post(f"/api/servers/s1/keys/{kid}/disable")).status_code == 404
    assert (await c.post(f"/api/servers/s1/keys/{kid}/extend",
                         json={"days": 30})).status_code == 404
    assert (await c.post(f"/api/servers/s1/keys/{kid}/sub")).status_code == 404
    assert (await c.delete(f"/api/servers/s1/keys/{kid}")).status_code == 404
    # and it really is untouched
    assert (await deps.db.get_key("s1", kid))["name"] == "Owner's"
    await owner.aclose()
    await c.aclose()


async def test_owner_transfers_a_user_onto_an_admins_page(app):
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "Handover", "limit_gb": 0, "days": 0})).json()["id"]
    aid = await _mk_sub(deps, username="a", caps="keys.view,keys.edit", servers="s1")
    c = await _login(application, "a", "sara-pw")
    assert (await c.get("/api/keys")).json()["keys"] == []          # not hers yet

    r = await owner.put(f"/api/servers/s1/keys/{kid}/owner", json={"admin_id": aid})
    assert r.status_code == 200 and r.json()["ownerAdminId"] == aid

    keys = (await c.get("/api/keys")).json()["keys"]                # now it is
    assert [k["name"] for k in keys] == ["Handover"]
    assert (await c.put(f"/api/servers/s1/keys/{kid}/name",
                        json={"name": "Renamed"})).status_code == 200

    # and back again
    assert (await owner.put(f"/api/servers/s1/keys/{kid}/owner",
                            json={"admin_id": None})).json()["ownerAdminId"] is None
    assert (await c.get("/api/keys")).json()["keys"] == []
    await owner.aclose()
    await c.aclose()


async def test_cannot_transfer_to_an_admin_without_access(app):
    """"If I've given them access, of course" — handing a user to an admin who
    cannot reach the server would strand it: invisible to them, gone from you."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "X", "limit_gb": 0, "days": 0})).json()["id"]
    berlin_only = await _mk_sub(deps, username="b", caps="keys.view", servers="s2")

    r = await owner.put(f"/api/servers/s1/keys/{kid}/owner", json={"admin_id": berlin_only})
    assert r.status_code == 400 and "access" in r.json()["detail"].lower()
    assert (await deps.db.get_key("s1", kid))["owner_admin_id"] is None   # unmoved
    assert (await owner.put(f"/api/servers/s1/keys/{kid}/owner",
                            json={"admin_id": 9999})).status_code == 404
    await owner.aclose()


async def test_only_the_owner_may_transfer(app):
    """Ownership decides who bills a customer, so a reseller must not reassign."""
    application, deps, _ = app
    a_id = await _mk_sub(deps, username="a", caps="keys.view,keys.create,keys.edit",
                         servers="s1")
    await _mk_sub(deps, username="b", caps="keys.view", servers="s1")
    c = await _login(application, "a", "sara-pw")
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Mine", "limit_gb": 0, "days": 0})).json()["id"]
    assert (await deps.db.get_key("s1", kid))["owner_admin_id"] == a_id

    r = await c.put(f"/api/servers/s1/keys/{kid}/owner", json={"admin_id": None})
    assert r.status_code == 403
    assert (await deps.db.get_key("s1", kid))["owner_admin_id"] == a_id
    await c.aclose()


async def test_a_mirrored_sub_belongs_to_whoever_owns_the_primary(app):
    application, deps, _ = app
    aid = await _mk_sub(deps, username="a", caps="keys.view,keys.create,keys.edit",
                        servers="s1,s2")
    c = await _login(application, "a", "sara-pw")
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Multi", "limit_gb": 0, "days": 0})).json()["id"]
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    assert (await c.post(f"/api/sub/{token}/servers/s2")).status_code == 200

    members = await deps.db.get_keys_by_sub_token(token)
    assert len(members) == 2
    assert all(m["owner_admin_id"] == aid for m in members)   # both hers
    assert len((await c.get("/api/keys")).json()["keys"]) == 2
    await c.aclose()


async def test_ownership_survives_a_backup_roundtrip(app):
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    aid = await _mk_sub(deps, username="a", caps="keys.view", servers="s1")
    kid = (await owner.post("/api/servers/s1/keys",
                            json={"name": "X", "limit_gb": 0, "days": 0})).json()["id"]
    await owner.put(f"/api/servers/s1/keys/{kid}/owner", json={"admin_id": aid})

    dump = (await owner.get("/api/backup")).json()
    assert (await owner.post("/api/restore", json=dump)).status_code == 200
    assert (await deps.db.get_key("s1", kid))["owner_admin_id"] == aid
    await owner.aclose()
