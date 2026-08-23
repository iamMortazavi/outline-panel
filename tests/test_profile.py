"""
The customer profile host, and rotating a customer's config.

The dangerous part is rotation: Outline counts usage per key, so handing a
customer a fresh key hands back their whole allowance unless the bytes already
spent are carried across.
"""
import os
import sys
import tempfile

import httpx
import pytest

from test_features import FakeOutline

GB = 1024 ** 3


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["TRUST_PROXY"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.dirname(__file__))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    # two servers: the multi-server tests need somewhere to mirror onto, and
    # `fake` stays s1 so the single-server tests read unchanged
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


async def _c(application, user="admin", pw="pw", login=True):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://panel.example.com")
    if login:
        assert (await c.post("/api/login",
                             json={"username": user, "password": pw})).status_code == 200
    return c


async def _key(c, name="Ali", gb=50, days=30):
    r = await c.post("/api/servers/s1/keys",
                     json={"name": name, "limit_gb": gb, "days": days})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------------------ tokens
def test_the_token_carries_a_readable_prefix():
    from outline_panel.core import security
    tok = security.profile_token("230")
    assert tok.startswith("230-")
    # the secret half stays full length whatever the prefix is
    assert len(tok.split("-", 1)[1]) >= 12
    assert security.profile_token("230") != security.profile_token("230")


def test_a_hostile_key_id_cannot_shape_the_token():
    """The id reaches this from Outline; it must not be able to inject a slash
    and turn the token into a path."""
    from outline_panel.core import security
    tok = security.profile_token("../../etc/passwd")
    assert "/" not in tok and ".." not in tok


# ------------------------------------------------------------ rotation
async def test_rotation_carries_the_used_bytes(app):
    """The hole this closes: a fresh key starts at zero usage, so without the
    carry-over the customer gets their whole allowance back for free."""
    application, deps, fakes = app
    fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c, gb=50)
    fake.usage[kid] = 30 * GB                     # 30 of 50 GB spent

    r = await c.post(f"/api/servers/s1/keys/{kid}/rotate")
    assert r.status_code == 200, r.text
    body = r.json()
    new_kid = body["id"]
    assert new_kid != kid
    assert body["carriedUsed"] == 30 * GB
    # 20 GB remained, so the new key's ceiling is 20 GB — not 50
    assert body["limit"] == 20 * GB
    assert (await deps.db.get_key("s1", new_kid))["limit_bytes"] == 20 * GB
    assert fake.limits[new_kid] == 20 * GB
    await c.aclose()


async def test_rotation_keeps_the_profile_link(app):
    """The whole point: the customer re-opens the link they already have."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]

    new_kid = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()["id"]
    assert (await deps.db.get_key_by_sub_token(token))["key_id"] == new_kid
    pub = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                            base_url="http://panel.example.com")
    assert (await pub.get(f"/sub/{token}/info")).status_code == 200
    await pub.aclose()
    await c.aclose()


async def test_rotation_does_not_restart_the_clock(app):
    """A rotation is not a renewal — the validity the customer has been running
    down has to survive it."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c, days=30)
    await deps.db.activate("s1", kid, 1_700_000_000, 1_700_500_000)

    new_kid = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()["id"]
    row = await deps.db.get_key("s1", new_kid)
    assert row["activated_ts"] == 1_700_000_000
    assert row["expiry_ts"] == 1_700_500_000
    assert row["duration_days"] == 30
    await c.aclose()


async def test_rotation_keeps_owner_monthly_and_disabled_state(app):
    application, deps, fakes = app
    fake = fakes["s1"]
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    aid = await deps.db.add_admin("sara", h, s, caps="keys.view,keys.edit",
                                  servers="s1")
    c = await _c(application)
    kid = await _key(c)
    await deps.db.set_key_owner("s1", kid, aid)
    await deps.db.set_monthly("s1", kid, 10 * GB, 1_700_000_000)
    await c.post(f"/api/servers/s1/keys/{kid}/disable")

    new_kid = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()["id"]
    row = await deps.db.get_key("s1", new_kid)
    assert row["owner_admin_id"] == aid
    assert row["monthly_bytes"] == 10 * GB and row["reset_ts"] == 1_700_000_000
    assert row["disabled"] == 1
    assert fake.limits[new_kid] == 0        # still cut off on Outline
    await c.aclose()


async def test_an_unlimited_key_stays_unlimited(app):
    application, deps, fakes = app
    fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c, gb=0)
    fake.usage[kid] = 900 * GB
    body = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()
    assert body["limit"] is None
    assert (await deps.db.get_key("s1", body["id"]))["limit_bytes"] is None
    await c.aclose()


async def test_a_key_already_over_its_ceiling_rotates_to_zero(app):
    """Not a negative limit — Outline reads 0 as blocked, which is the state it
    was already in."""
    application, deps, fakes = app
    fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c, gb=10)
    fake.usage[kid] = 25 * GB
    body = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()
    assert body["limit"] == 0
    await c.aclose()


async def test_the_old_key_is_gone_and_only_one_remains(app):
    application, deps, fakes = app
    fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    new_kid = (await c.post(f"/api/servers/s1/keys/{kid}/rotate")).json()["id"]
    assert kid not in fake.keys and new_kid in fake.keys
    assert await deps.db.get_key("s1", kid) is None
    assert len(await deps.db.all_keys()) == 1
    await c.aclose()


async def test_a_failure_leaves_the_customer_connected(app):
    """The new key is created before the old one is deleted, so a failure must
    not take away the config they already had."""
    application, deps, fakes = app
    fake = fakes["s1"]
    from outline_panel.core.outline_api import OutlineError
    c = await _c(application)
    kid = await _key(c)

    async def refuse(*a, **kw):
        raise OutlineError("server unreachable")

    fake.create_key = refuse
    r = await c.post(f"/api/servers/s1/keys/{kid}/rotate")
    assert r.status_code == 502
    assert kid in fake.keys                       # still theirs
    assert await deps.db.get_key("s1", kid) is not None
    await c.aclose()


async def test_rotation_needs_keys_edit_and_ownership(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view", servers="s1")
    owner = await _c(application)
    kid = await _key(owner)

    sara = await _c(application, "sara", "sara-pw")
    assert (await sara.post(f"/api/servers/s1/keys/{kid}/rotate")).status_code in (403, 404)
    assert await deps.db.get_key("s1", kid) is not None
    await sara.aclose()
    await owner.aclose()


async def test_rotation_is_audited(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    await c.post(f"/api/servers/s1/keys/{kid}/rotate")
    entry = (await deps.db.audit_page(limit=1))[0]
    assert entry["action"].endswith("/rotate") and entry["status"] == 200
    assert entry["actor_name"] == "admin"
    await c.aclose()


# ------------------------------------------------------- the profile host
async def test_the_profile_host_serves_only_the_profile(app):
    """This URL goes to every customer and gets forwarded. Whoever ends up with
    it must not also be holding the address of the admin panel."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    r = await c.put("/api/settings/profile",
                    json={"baseUrl": "https://star.example.com"})
    assert r.status_code == 200 and r.json()["host"] == "star.example.com"
    await c.aclose()

    star = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://star.example.com")
    assert (await star.get(f"/{token}")).status_code == 200
    assert (await star.get(f"/{token}/info")).status_code == 200
    assert (await star.get(f"/sub/{token}/info")).status_code == 200
    # ...and nothing else
    for path in ("/", "/api/me", "/api/keys", "/tma", "/metrics", "/api/audit"):
        r = await star.get(path)
        assert r.status_code == 404, f"{path} answered {r.status_code} on the profile host"
    assert (await star.post("/api/login",
                            json={"username": "admin", "password": "pw"})).status_code == 404
    await star.aclose()


# /healthz is deliberately absent: it is allowed on the profile host so uptime
# monitoring can reach it, and it reveals nothing but {"ok": true}.
@pytest.mark.parametrize("path", ["/metrics", "/openapi.json",
                                  "/docs", "/api", "/static", "/tma"])
async def test_a_route_name_is_never_mistaken_for_a_token(app, path):
    """A token is recognised by shape, and several real route names share it —
    "metrics" is seven alphanumerics. Those must not slip past the guard."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    await c.put("/api/settings/profile", json={"baseUrl": "https://star.example.com"})
    await c.aclose()
    star = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://star.example.com")
    r = await star.get(path)
    assert r.status_code == 404, f"{path} answered {r.status_code}"
    await star.aclose()


async def test_the_panel_host_is_untouched(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    await c.put("/api/settings/profile", json={"baseUrl": "https://star.example.com"})
    assert (await c.get("/api/me")).status_code == 200
    assert (await c.get("/")).status_code == 200
    await c.aclose()


async def test_the_profile_url_is_handed_to_the_reseller(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    await c.put("/api/settings/profile", json={"baseUrl": "https://star.example.com"})
    body = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()
    assert body["profileUrl"] == f"https://star.example.com/{body['token']}"
    assert body["token"].startswith(f"{kid}-")
    await c.aclose()


async def test_without_a_profile_host_nothing_changes(app):
    """Unset is the default, and must leave the panel exactly as it was."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    kid = await _key(c)
    body = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()
    assert body["profileUrl"] is None
    assert (await c.get("/api/me")).status_code == 200
    assert (await c.get("/")).status_code == 200
    await c.aclose()


@pytest.mark.parametrize("bad", ["star.example.com", "ftp://x.com",
                                 "https://star.example.com/some/path", "not a url"])
async def test_the_profile_url_must_be_a_real_origin(app, bad):
    """A bare hostname parses with no hostname at all, which would silently gate
    nothing — the setting would look applied and do nothing."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    assert (await c.put("/api/settings/profile", json={"baseUrl": bad})).status_code == 400
    await c.aclose()


async def test_clearing_it_restores_one_host_mode(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    await c.put("/api/settings/profile", json={"baseUrl": "https://star.example.com"})
    assert (await c.put("/api/settings/profile", json={"baseUrl": ""})).json()["host"] == ""
    star = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://star.example.com")
    # 401, not 404: the route is reachable again — the guard is what 404s
    assert (await star.get("/api/me")).status_code == 401
    assert (await star.get("/")).status_code == 200
    await star.aclose()
    await c.aclose()


# ------------------------------------------------- every key gets a link
async def test_a_new_key_gets_its_link_immediately(app):
    """A link the reseller has to remember to generate is a link most customers
    never receive."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    r = await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 5})
    body = r.json()
    assert body["subToken"], "created without a customer link"
    assert body["subToken"].startswith(f"{body['id']}-")
    row = await deps.db.get_key("s1", body["id"])
    assert row["sub_token"] == body["subToken"]
    await c.aclose()


async def test_the_key_list_carries_the_link(app):
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    await c.put("/api/settings/profile", json={"baseUrl": "https://star.example.com"})
    _kid = await _key(c)
    k = (await c.get("/api/keys")).json()["keys"][0]
    assert k["profileUrl"] == f"https://star.example.com/{k['subToken']}"
    await c.aclose()


async def test_without_a_profile_host_the_link_is_a_path(app):
    """Still usable — the browser resolves it against the panel's own origin."""
    application, deps, fakes = app
    _fake = fakes["s1"]
    c = await _c(application)
    _kid = await _key(c)
    k = (await c.get("/api/keys")).json()["keys"][0]
    assert k["profileUrl"] == f"/sub/{k['subToken']}"
    await c.aclose()


async def test_the_backfill_gives_every_old_key_a_link():
    """A panel upgrading into this feature has a table full of customers with no
    token. The migration is what stops that being a manual job per key."""
    import sqlite3

    from outline_panel.core.db import _MIGRATIONS, DB
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE keys (server_id TEXT, key_id TEXT, name TEXT, limit_bytes INTEGER,
            duration_days INTEGER, activated_ts INTEGER, expiry_ts INTEGER,
            disabled INTEGER DEFAULT 0, monthly_bytes INTEGER, reset_ts INTEGER,
            sub_token TEXT, created_ts INTEGER, owner_admin_id INTEGER,
            PRIMARY KEY (server_id, key_id));
        INSERT INTO keys (server_id,key_id,name,sub_token) VALUES ('s1','1','a',NULL);
        INSERT INTO keys (server_id,key_id,name,sub_token) VALUES ('s1','2','b',NULL);
        INSERT INTO keys (server_id,key_id,name,sub_token) VALUES ('s1','3','c','shared-token');
        INSERT INTO keys (server_id,key_id,name,sub_token) VALUES ('s2','4','c','shared-token');
    """)
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()
    assert await db.schema_version() == len(_MIGRATIONS)
    rows = {(k["server_id"], k["key_id"]): k["sub_token"] for k in await db.all_keys()}
    assert all(rows.values()), "a key was left without a link"
    assert rows[("s1", "1")].startswith("1-") and rows[("s1", "2")].startswith("2-")
    assert rows[("s1", "1")] != rows[("s1", "2")]
    # an existing shared token ties a multi-server subscription together and
    # must survive untouched
    assert rows[("s1", "3")] == "shared-token" == rows[("s2", "4")]
    await db.close()


async def test_a_link_never_changes_on_restart():
    """Once issued, a link is the customer's address. Re-running init must not
    reissue it — a changed link is a customer who can no longer reach their
    config, and they would have no way to know."""
    import sqlite3

    from outline_panel.core.db import DB
    path = os.path.join(tempfile.mkdtemp(), "x.db")
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE keys (server_id TEXT, key_id TEXT, name TEXT, limit_bytes INTEGER,
            duration_days INTEGER, activated_ts INTEGER, expiry_ts INTEGER,
            disabled INTEGER DEFAULT 0, monthly_bytes INTEGER, reset_ts INTEGER,
            sub_token TEXT, created_ts INTEGER, owner_admin_id INTEGER,
            PRIMARY KEY (server_id, key_id));
        INSERT INTO keys (server_id,key_id,name,sub_token) VALUES ('s1','7','Ali',NULL);
    """)
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()                       # backfill issues one
    first = (await db.get_key("s1", "7"))["sub_token"]
    await db.close()
    assert first and first.startswith("7-")

    for _ in range(3):                    # restarts must not touch it
        db = DB(path)
        await db.init()
        assert (await db.get_key("s1", "7"))["sub_token"] == first
        await db.close()


async def test_an_adopted_key_also_gets_a_link(app):
    """A key made straight from Outline Manager has no row here until someone
    edits it. It must not end up a quiet second class with no page."""
    application, deps, fakes = app
    fake = fakes["s1"]
    c = await _c(application)
    made = await fake.create_key(name="made-outside")
    assert await deps.db.get_key("s1", made["id"]) is None
    r = await c.put(f"/api/servers/s1/keys/{made['id']}/name", json={"name": "Sara"})
    assert r.status_code == 200
    row = await deps.db.get_key("s1", made["id"])
    assert row["sub_token"], "adopted key has no customer link"
    assert row["sub_token"].startswith(f"{made['id']}-")
    await c.aclose()


# ------------------------------------------- several servers from the start
async def test_creating_on_several_servers_gives_one_link(app):
    """One subscription, one link, a config on each — the whole point of asking
    at creation instead of making it a second trip through the panel."""
    application, deps, fakes = app
    c = await _c(application)
    r = await c.post("/api/servers/s1/keys",
                     json={"name": "Ali", "limit_gb": 50, "days": 30,
                           "extra_servers": ["s2"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["servers"] == ["s1", "s2"]
    assert "serverErrors" not in body

    members = await deps.db.get_keys_by_sub_token(body["subToken"])
    assert {m["server_id"] for m in members} == {"s1", "s2"}
    # the mirror carries the primary's allowance, deliberately undivided
    assert all(m["limit_bytes"] == 50 * GB for m in members)
    assert all(m["duration_days"] == 30 for m in members)
    await c.aclose()


async def test_the_customer_page_lists_every_server(app):
    application, deps, fakes = app
    c = await _c(application)
    body = (await c.post("/api/servers/s1/keys",
                         json={"name": "Ali", "limit_gb": 10,
                               "extra_servers": ["s2"]})).json()
    pub = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                            base_url="http://panel.example.com")
    info = (await pub.get(f"/sub/{body['subToken']}/info")).json()
    assert len(info["servers"]) == 2
    assert {s["server"] for s in info["servers"]} == {"Tokyo", "Berlin"}
    await pub.aclose()
    await c.aclose()


async def test_an_unreachable_extra_server_does_not_undo_the_sale(app):
    """The customer already has a working config and, on credit, the package is
    already paid for. Unwinding that because one server blipped is worse than
    handing back a key that works on the others."""
    application, deps, fakes = app
    from outline_panel.core.outline_api import OutlineError

    async def refuse(*a, **kw):
        raise OutlineError("unreachable")

    fakes["s2"].create_key = refuse
    c = await _c(application)
    r = await c.post("/api/servers/s1/keys",
                     json={"name": "Ali", "limit_gb": 5, "extra_servers": ["s2"]})
    assert r.status_code == 200
    body = r.json()
    assert body["servers"] == ["s1"]
    assert [e["id"] for e in body["serverErrors"]] == ["s2"]
    assert await deps.db.get_key("s1", body["id"]) is not None
    await c.aclose()


async def test_extra_servers_are_scoped_to_the_admin(app):
    """A sub-admin cannot use this to mint a key on a server they were never
    given — the same rule sub_add_server enforces by hand."""
    application, deps, fakes = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view,keys.create,keys.edit",
                            servers="s1")
    c = await _c(application, "sara", "sara-pw")
    body = (await c.post("/api/servers/s1/keys",
                         json={"name": "Ali", "limit_gb": 5,
                               "extra_servers": ["s2"]})).json()
    assert body["servers"] == ["s1"]
    assert [e["id"] for e in body["serverErrors"]] == ["s2"]
    assert not await deps.db.keys_for("s2")
    await c.aclose()


async def test_a_repeated_or_self_referencing_server_is_ignored(app):
    application, deps, fakes = app
    c = await _c(application)
    body = (await c.post("/api/servers/s1/keys",
                         json={"name": "Ali", "limit_gb": 5,
                               "extra_servers": ["s1", "s2", "s2"]})).json()
    assert body["servers"] == ["s1", "s2"]
    assert len(await deps.db.get_keys_by_sub_token(body["subToken"])) == 2
    await c.aclose()
