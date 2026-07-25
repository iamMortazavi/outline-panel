"""
Regressions for the review fixes: bot scope/ownership, the credit bypasses,
fail-closed rights, live settings and the registry no longer going stale.

Each test names the hole it closes, so a future "simplification" that reopens
one fails here rather than in production.
"""
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


async def _mk_sub(deps, username="sara", caps="keys.view,keys.edit,keys.create",
                  servers="s1", credit=0):
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    aid = await deps.db.add_admin(username, h, s, caps=caps, servers=servers)
    if credit:
        await deps.db.update_admin(aid, credit_enabled=1)
        await deps.db.credit_admin(aid, credit, reason="topup")
    return aid


# ------------------------------------------------------- bot scope + ownership
class _Answered(Exception):
    """Raised by the fake Telegram objects so a test can see the bot refused."""


class _FakeCq:
    """Just enough CallbackQuery for a handler to run."""

    def __init__(self, uid, data):
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers = []
        self.message = self

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)

    async def edit_text(self, *a, **kw):
        self.edited = True

    async def edit_reply_markup(self, *a, **kw):
        self.edited = True


def _handler(dp, data):
    """The callback handler whose filter matches `data`."""
    for h in dp.callback_query.handlers:
        for f in h.filters or ():
            cb = getattr(f, "callback", None)
            try:
                if cb is not None and cb(type("E", (), {"data": data})()):
                    return h.callback
            except Exception:  # noqa: BLE001 — a filter that can't judge this event
                continue
    raise AssertionError(f"no handler for {data!r}")


async def _bot(deps):
    from outline_panel.bot.dispatcher import build_dispatcher
    return build_dispatcher(deps.db, deps.reg, lambda: set(),
                            resolve_admin=deps.settings.admin_for_telegram)


async def test_bot_refuses_a_key_on_an_out_of_scope_server(app):
    """callback_data is composed by the client. A sub-admin scoped to s1 could
    forge `disable:s2:<kid>` and reach another reseller's customer, because the
    handlers checked the capability and nothing else."""
    application, deps, fakes = app
    aid = await _mk_sub(deps, servers="s1")
    await deps.db.update_admin(aid, telegram_id=555)
    # a key on s2, which this admin cannot see
    key = await fakes["s2"].create_key(name="theirs")
    await deps.db.add_key("s2", key["id"], "theirs", None, None)

    dp = await _bot(deps)
    for action in ("key", "link", "disable", "enable", "extend", "rename", "limit"):
        cq = _FakeCq(555, f"{action}:s2:{key['id']}")
        await _handler(dp, cq.data)(cq, **({"state": _FakeState()}
                                           if action in ("rename", "limit") else {}))
        assert any("Unknown server" in (a or "") for a in cq.answers), action
    # and the key was left alone
    assert not (await deps.db.get_key("s2", key["id"]))["disabled"]


async def test_bot_refuses_another_admins_key_on_a_shared_server(app):
    """Same server, different owner: ownership is the second half of the check."""
    application, deps, fakes = app
    mine = await _mk_sub(deps, "sara", servers="s1")
    theirs = await _mk_sub(deps, "reza", servers="s1")
    await deps.db.update_admin(mine, telegram_id=555)
    key = await fakes["s1"].create_key(name="reza's")
    await deps.db.add_key("s1", key["id"], "reza's", None, None, owner_admin_id=theirs)

    dp = await _bot(deps)
    cq = _FakeCq(555, f"disable:s1:{key['id']}")
    await _handler(dp, cq.data)(cq)
    assert any("Unknown user" in (a or "") for a in cq.answers)
    assert not (await deps.db.get_key("s1", key["id"]))["disabled"]


async def test_bot_will_not_extend_or_resize_for_free(app):
    """A credit admin buys everything a customer gets. The bot's 'Extend 30
    days' and 'Data limit' buttons handed both out for nothing."""
    application, deps, fakes = app
    aid = await _mk_sub(deps, servers="s1", credit=100_000)
    await deps.db.update_admin(aid, telegram_id=555)
    key = await fakes["s1"].create_key(name="mine")
    await deps.db.add_key("s1", key["id"], "mine", None, 30, owner_admin_id=aid)

    dp = await _bot(deps)
    for action in ("extend", "limit"):
        cq = _FakeCq(555, f"{action}:s1:{key['id']}")
        await _handler(dp, cq.data)(cq, **({"state": _FakeState()}
                                           if action == "limit" else {}))
        assert any("package" in (a or "") for a in cq.answers), action
    assert (await deps.db.get_key("s1", key["id"]))["expiry_ts"] is None


class _FakeState:
    def __init__(self):
        self.data = {}

    async def set_state(self, *a):
        pass

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data = {}


# ------------------------------------------------------- credit bypass (panel)
async def test_credit_admin_cannot_top_up_a_key_for_free(app):
    """limit / monthly / reset / sub-mirror all handed a customer more product
    without touching the balance — the price list was only enforced on create
    and extend."""
    application, deps, fakes = app
    aid = await _mk_sub(deps, servers="s1,s2", credit=100_000)
    key = await fakes["s1"].create_key(name="mine")
    await deps.db.add_key("s1", key["id"], "mine", 1024, 30, owner_admin_id=aid)
    c = await _login(application, "sara", "sara-pw")
    kid = key["id"]

    assert (await c.put(f"/api/servers/s1/keys/{kid}/limit",
                        json={"limit_gb": 9999})).status_code == 403
    assert (await c.put(f"/api/servers/s1/keys/{kid}/monthly",
                        json={"monthly_gb": 500})).status_code == 403
    assert (await c.post(f"/api/servers/s1/keys/{kid}/reset")).status_code == 403
    # the stored allowance never moved
    assert (await deps.db.get_key("s1", kid))["limit_bytes"] == 1024

    sub = await c.post(f"/api/servers/s1/keys/{kid}/sub")
    assert sub.status_code == 200
    token = sub.json()["token"]
    # a mirror is a whole second key with the same allowance, and it was free
    assert (await c.post(f"/api/sub/{token}/servers/s2")).status_code == 403
    assert len(await deps.db.get_keys_by_sub_token(token)) == 1
    await c.aclose()


async def test_the_owner_and_exempt_admins_still_use_those_routes(app):
    """The block is about the price list, not about the routes being dangerous."""
    application, deps, fakes = app
    key = await fakes["s1"].create_key(name="mine")
    await deps.db.add_key("s1", key["id"], "mine", 1024, 30)
    c = await _login(application, "admin", "pw")
    r = await c.put(f"/api/servers/s1/keys/{key['id']}/limit", json={"limit_gb": 50})
    assert r.status_code == 200
    await c.aclose()


# --------------------------------------------------------------- fail-closed
async def test_an_empty_server_list_grants_nothing(app):
    """It used to mean 'every server' — the panel's widest grant reachable by
    leaving a field blank, or by restoring a backup written before scoping."""
    application, deps, fakes = app
    await _mk_sub(deps, servers="")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/servers")).json()["servers"] == []
    assert (await c.get("/api/keys")).json()["keys"] == []
    assert (await c.get("/api/keys?server=s1")).status_code == 404
    await c.aclose()


async def test_a_rejected_admin_leaves_no_row_behind(app):
    """The duplicate-telegram check ran after the INSERT, so every failed
    attempt still created an admin."""
    application, deps, _ = app
    first = await _mk_sub(deps, "sara")
    await deps.db.update_admin(first, telegram_id=777)
    c = await _login(application, "admin", "pw")
    before = len(await deps.db.all_admins())
    r = await c.post("/api/admins", json={"username": "reza", "password": "secret1",
                                          "caps": ["keys.view"], "servers": ["s1"],
                                          "telegram_id": 777})
    assert r.status_code == 400
    assert len(await deps.db.all_admins()) == before
    assert await deps.db.get_admin_by_username("reza") is None
    await c.aclose()


# ------------------------------------------------------------- live settings
async def test_panel_knobs_are_readable_editable_and_validated(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    r = await c.get("/api/settings/panel")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["cycle_days"] == 30
    assert any(s["key"] == "metrics_ttl" for s in body["spec"])

    assert (await c.put("/api/settings/panel", json={"cycle_days": 7})).status_code == 200
    assert await deps.settings.num("cycle_days") == 7
    assert await deps.settings.cycle_seconds() == 7 * 86400

    # out of range and unknown keys are refused, not silently dropped
    assert (await c.put("/api/settings/panel", json={"cycle_days": 0})).status_code == 400
    assert (await c.put("/api/settings/panel", json={"nope": 1})).status_code == 400
    assert await deps.settings.num("cycle_days") == 7

    # 0 is a legal metrics TTL: it means "never cache"
    assert (await c.put("/api/settings/panel", json={"metrics_ttl": 0})).status_code == 200
    assert await deps.settings.num("metrics_ttl") == 0

    assert (await c.post("/api/settings/panel/reset")).status_code == 200
    assert await deps.settings.num("cycle_days") == 30
    await c.aclose()


async def test_a_corrupt_stored_knob_falls_back_to_its_default(app):
    """A hand-edited interval of 0 would spin the scheduler flat out against
    every Outline server."""
    _, deps, _ = app
    await deps.db.set_setting("expiry_check_interval", "0")
    assert await deps.settings.num("expiry_check_interval") == 60
    await deps.db.set_setting("expiry_check_interval", "not-a-number")
    assert await deps.settings.num("expiry_check_interval") == 60


async def test_panel_knobs_are_owner_only(app):
    application, deps, _ = app
    await _mk_sub(deps, servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/settings/panel")).status_code == 403
    assert (await c.put("/api/settings/panel", json={"cycle_days": 1})).status_code == 403
    await c.aclose()


async def test_settings_are_not_cached_across_stores(app):
    """The store used to memoise per process, so the standalone bot kept polling
    with a token the panel had already replaced."""
    _, deps, _ = app
    from outline_panel.core.settings import BOT_TOKEN, SettingsStore
    other = SettingsStore(deps.db)          # a second process's view
    await deps.settings.set(BOT_TOKEN, "first")
    assert await other.get(BOT_TOKEN) == "first"
    await other.set(BOT_TOKEN, "second")
    assert await deps.settings.get(BOT_TOKEN) == "second"


# ------------------------------------------------------------------ registry
async def test_the_registry_follows_the_database(app):
    """It was read once at startup, so a server added by another worker stayed
    invisible and a deleted one stayed usable."""
    _, deps, fakes = app
    live = deps.reg.get("s1")
    await deps.reg.sync()
    assert deps.reg.get("s1") is live, "an unchanged row must not rebuild the client"

    await deps.db.add_server("s3", "Oslo", "https://9.9.9.9:1/x")
    await deps.reg.sync()
    assert "s3" in deps.reg.ids() and deps.reg.meta("s3")["name"] == "Oslo"

    await deps.db.rename_server_local("s3", "Bergen")
    await deps.reg.sync()
    assert deps.reg.meta("s3")["name"] == "Bergen"

    await deps.db.delete_server("s3")
    await deps.reg.sync()
    assert "s3" not in deps.reg.ids()


# ----------------------------------------------------------- own password
async def test_a_sub_admin_can_rotate_their_own_password(app):
    """There was no endpoint they could reach: /api/settings/password and
    /api/admins/{id} are both owner-only, so a reseller's password could only
    be changed by someone who then had to tell them what it was."""
    application, deps, _ = app
    await _mk_sub(deps, servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.post("/api/me/password",
                         json={"current": "wrong", "new": "brand-new"})).status_code == 401
    assert (await c.post("/api/me/password",
                         json={"current": "sara-pw", "new": "brand-new"})).status_code == 200
    await c.aclose()

    assert await deps.settings.verify_login("sara", "sara-pw") is None
    assert await deps.settings.verify_login("sara", "brand-new") is not None


async def test_the_owner_rotates_theirs_where_it_actually_lives(app):
    """The owner's password is in `settings`, not in their admins row — writing
    it to the row would leave them logging in with the old one."""
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    assert (await c.post("/api/me/password",
                         json={"current": "pw", "new": "new-owner-pw"})).status_code == 200
    await c.aclose()
    assert await deps.settings.verify_admin_password("new-owner-pw")
    c2 = await _login(application, "admin", "new-owner-pw")
    await c2.aclose()


# --------------------------------------------------------- the user's page
async def test_a_plan_that_has_not_started_does_not_read_as_never_expiring(app):
    """expiry_ts is NULL until the first connection, so the customer's page had
    nothing to show and rendered "No expiry" — to someone who just bought 30
    days. The term has to survive to the page, not just the countdown."""
    _, deps, fakes = app
    key = await fakes["s1"].create_key(name="Ali")
    await deps.db.add_key("s1", key["id"], "Ali", None, 30)
    await deps.db.set_sub_token("s1", key["id"], "tok-pending")

    # the uncached form: this asserts on the computation, and activation is a
    # scheduler write that the public cache is allowed to lag behind
    from outline_panel.web.routers.subscription import _collect_fresh
    info = await _collect_fresh("tok-pending")
    assert info["expire"] == 0            # genuinely unknown until they connect
    assert info["pendingDays"] == 30      # ...but the term is not

    # once it activates, the real date takes over and nothing is "pending"
    await deps.db.activate("s1", key["id"], 1_700_000_000, 1_700_086_400)
    info = await _collect_fresh("tok-pending")
    assert info["expire"] == 1_700_086_400 and info["pendingDays"] == 0


async def test_a_key_with_no_term_at_all_is_still_unlimited(app):
    """No duration means no expiry — that one really does never end, and must
    not start claiming a pending term."""
    _, deps, fakes = app
    key = await fakes["s1"].create_key(name="Sara")
    await deps.db.add_key("s1", key["id"], "Sara", None, None)
    await deps.db.set_sub_token("s1", key["id"], "tok-forever")
    from outline_panel.web.routers.subscription import _collect_fresh
    info = await _collect_fresh("tok-forever")
    assert info["expire"] == 0 and info["pendingDays"] == 0


# ------------------------------------------------------------- notifications
async def test_an_alert_goes_to_the_admin_who_owns_the_key(app):
    """Every alert used to go to the global bot admin list: the owner got every
    reseller's warnings and the reseller got none of their own."""
    _, deps, _ = app
    aid = await _mk_sub(deps, servers="s1")
    await deps.db.update_admin(aid, telegram_id=999)
    deps.botmgr.get_admin_ids = lambda: {111}

    assert await deps.botmgr._targets(aid) == {999}
    assert await deps.botmgr._targets(None) == {111}     # the owner's own key
    # an admin with no Telegram account linked must not silently lose the alert
    unlinked = await _mk_sub(deps, "reza", servers="s1")
    assert await deps.botmgr._targets(unlinked) == {111}
