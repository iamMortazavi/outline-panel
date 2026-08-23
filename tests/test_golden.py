"""
Golden master: the wire contract, frozen.

No route in this panel declares a `response_model`; every response is a
hand-built dict and the frontend consumes it by convention. That is fine until
something moves the code, at which point a renamed field is invisible until the
UI blanks.

So: seed one fixed panel, call ~40 endpoints, normalise the volatile bits, and
snapshot. The re-architecture may move every line of `keys.py`; these files must
not change. When a change *is* intended, `UPDATE_GOLDEN=1 pytest` rewrites them
and the diff is what gets reviewed.

The seed deliberately covers the states that differ in shape, not just in value:
pending vs. activated, limited vs. unlimited, monthly vs. one-off, disabled,
mirrored across two servers, adopted from Outline with no panel row, and owned
by a sub-admin rather than the owner.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from test_features import FakeOutline

GB = 1024 ** 3
GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.getenv("UPDATE_GOLDEN") == "1"

# Anything time-shaped is replaced wholesale: the snapshot must not depend on
# when it was taken. The window is "plausible unix seconds", which no id, byte
# count or price in this panel falls into.
_TS_LOW, _TS_HIGH = 1_600_000_000, 4_000_000_000
_VOLATILE_KEYS = {"ts", "created_ts", "createdTs", "expiry_ts", "expiry",
                  "activated_ts", "reset_ts", "lastTs", "verified_ts",
                  "bwTs", "lastSeen", "next_try_ts", "expires_ts"}
_TOKENISH = {"token", "subToken", "sub_token", "profileUrl", "path", "uri",
             "secret", "holder", "pw_hash", "pw_salt"}


def _norm(value, key=None):
    """A copy of `value` with everything that legitimately varies pinned."""
    if isinstance(value, dict):
        return {k: _norm(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm(v, key) for v in value]
    if key in _VOLATILE_KEYS and isinstance(value, (int, float)) and value:
        return "<TS>"
    if key in _TOKENISH and isinstance(value, str) and value:
        # keep the shape, drop the randomness: "3-aB9xQ..." -> "3-<RAND>"
        return re.sub(r"[A-Za-z0-9_-]{12,}", "<RAND>", value)
    if isinstance(value, (int, float)) and _TS_LOW < value < _TS_HIGH:
        return "<TS>"
    # the DB (and therefore the backup dir) lives in a per-run tempdir
    if isinstance(value, str) and value.startswith(("/tmp/", "/var/folders/")):
        return "<PATH>"
    # a snapshot is named for the second it was taken in
    if isinstance(value, str):
        value = re.sub(r"outline-panel-\d{8}-\d{6}\.db", "outline-panel-<STAMP>.db", value)
    return value


def _check(name: str, status: int, body) -> None:
    """Compare one response against its snapshot, or write it."""
    path = GOLDEN / f"{name}.json"
    actual = {"status": status, "body": _norm(body)}
    if UPDATE or not path.exists():
        path.write_text(json.dumps(actual, indent=2, sort_keys=True,
                                   ensure_ascii=False) + "\n")
        if not UPDATE:
            pytest.fail(f"golden/{name}.json did not exist and was created — "
                        f"review it and commit it")
        return
    expected = json.loads(path.read_text())
    assert actual == expected, (
        f"\n{name}: the wire contract moved.\n"
        f"If that was deliberate: UPDATE_GOLDEN=1 pytest tests/test_golden.py\n"
        f"then read the diff before committing it."
    )


# --------------------------------------------------------------------- seed
@pytest.fixture
async def seeded():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "golden.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["TRUST_PROXY"] = "false"
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


async def _client(application, user="admin", pw="pw"):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                          base_url="http://panel.example.com")
    r = await c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return c


async def _seed(app_tuple):
    """Build the fixed panel. Returns (owner client, reseller client, ids)."""
    application, deps, fakes = app_tuple
    owner = await _client(application)
    now = int(time.time())

    # price list
    for name, gb, days, monthly, price in (
        ("Starter", 10, 30, None, 90_000),
        ("Standard", 50, 30, None, 250_000),
        ("Monthly 30", None, 30, 30, 300_000),
        ("Unlimited 90", None, 90, None, 900_000),
        ("No expiry 100", 100, None, None, 400_000),
    ):
        r = await owner.post("/api/packages", json={
            "name": name, "gb": gb, "days": days,
            "monthly_gb": monthly, "price": price})
        assert r.status_code == 200, r.text

    # two sub-admins: one free-form, one on credit with a discount
    r = await owner.post("/api/admins", json={
        "username": "sara", "password": "sara-pw", "caps": ["keys.view", "keys.create",
        "keys.edit", "keys.delete"], "servers": ["s1"], "credit_enabled": True,
        "discount_pct": 10, "credit": 5_000_000, "telegram_id": 111})
    assert r.status_code == 200, r.text
    sara_id = r.json()["id"]
    r = await owner.post("/api/admins", json={
        "username": "reza", "password": "reza-pw", "caps": ["keys.view"],
        "servers": ["s1", "s2"], "credit_enabled": False})
    assert r.status_code == 200, r.text

    # the owner's keys, one per interesting shape
    async def mk(**body):
        r = await owner.post("/api/servers/s1/keys", json=body)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    k_pending = await mk(name="pending-30d", limit_gb=10, days=30)
    k_active = await mk(name="active-now", limit_gb=50, days=30, start_now=True)
    k_unlimited = await mk(name="unlimited", limit_gb=0, days=0)
    k_monthly = await mk(name="monthly-30", limit_gb=0, days=0, monthly_gb=30)
    k_disabled = await mk(name="suspended", limit_gb=20, days=30, start_now=True)
    k_mirror = await mk(name="two-servers", limit_gb=40, days=60, start_now=True,
                        extra_servers=["s2"])
    k_expiring = await mk(name="expires-soon", limit_gb=15, days=2, start_now=True)

    await owner.post(f"/api/servers/s1/keys/{k_disabled}/disable")
    # a key made straight from Outline Manager: no panel row until it is touched
    adopted = await fakes["s1"].create_key(name="adopted-from-manager")
    # usage, so the bars and the subscription have something to report
    fakes["s1"].usage[k_active] = 12 * GB
    fakes["s1"].usage[k_monthly] = 3 * GB
    fakes["s1"].usage[k_mirror] = 7 * GB

    # the reseller's own customer, bought from the price list
    sara = await _client(application, "sara", "sara-pw")
    pkgs = (await sara.get("/api/packages")).json()["packages"]
    r = await sara.post("/api/servers/s1/keys",
                        json={"name": "sara-customer", "package_id": pkgs[0]["id"]},
                        headers={"Idempotency-Key": "golden-seed-1"})
    assert r.status_code == 200, r.text
    k_sara = r.json()["id"]

    # server health history, so /api/servers/health is not empty
    await deps.db.record_health("s1", True, 42, None)
    await deps.db.record_health("s2", False, None, "connection refused")

    token = (await deps.db.get_key("s1", k_mirror))["sub_token"]
    return owner, sara, {
        "pending": k_pending, "active": k_active, "unlimited": k_unlimited,
        "monthly": k_monthly, "disabled": k_disabled, "mirror": k_mirror,
        "expiring": k_expiring, "adopted": adopted["id"], "sara": k_sara,
        "sara_admin": sara_id, "token": token, "now": now,
    }


# --------------------------------------------------------------------- reads
async def test_golden_owner_reads(seeded):
    """Every read surface the dashboard depends on, as the owner."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    cases = [
        ("me", "/api/me"),
        ("me_ledger", "/api/me/ledger"),
        ("me_2fa", "/api/me/2fa"),
        ("servers", "/api/servers"),
        ("servers_health", "/api/servers/health"),
        ("server_s1_health", "/api/servers/s1/health"),
        ("server_s1_settings", "/api/servers/s1/settings"),
        ("keys", "/api/keys"),
        ("keys_one_server", "/api/keys?server=s1"),
        ("stats", "/api/stats"),
        ("packages", "/api/packages"),
        ("admins", "/api/admins"),
        ("admin_ledger", f"/api/admins/{ids['sara_admin']}/ledger"),
        ("settings", "/api/settings"),
        ("settings_panel", "/api/settings/panel"),
        ("settings_profile", "/api/settings/profile"),
        ("settings_bot", "/api/settings/bot"),
        ("snapshots", "/api/snapshots"),
        ("healthz", "/healthz"),
    ]
    for name, url in cases:
        r = await owner.get(url)
        _check(f"owner_{name}", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_reseller_reads(seeded):
    """The same surfaces as a scoped, credit-enabled sub-admin. Shape *and*
    filtering: this is where A-2 (own customers only) and A-1 (scope) show."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    for name, url in [
        ("me", "/api/me"),
        ("servers", "/api/servers"),
        ("keys", "/api/keys"),
        ("stats", "/api/stats"),
        ("packages", "/api/packages"),
        ("me_ledger", "/api/me/ledger"),
    ]:
        r = await sara.get(url)
        _check(f"reseller_{name}", r.status_code, r.json())
    # and the surfaces she must not reach at all
    for name, url in [("admins", "/api/admins"), ("audit", "/api/audit"),
                      ("backup", "/api/backup"), ("panel", "/api/settings/panel")]:
        r = await sara.get(url)
        _check(f"reseller_denied_{name}", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_public_subscription(seeded):
    """The customer-facing surface. Its shape is a contract with VPN clients,
    not just with our own page — Subscription-Userinfo is parsed by v2rayNG."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    token = ids["token"]
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                          base_url="http://panel.example.com")
    r = await c.get(f"/sub/{token}/info", headers={"accept": "*/*"})
    _check("public_sub_info", r.status_code, r.json())
    r = await c.get(f"/sub/{token}", headers={"accept": "*/*"})
    # `expire=` inside the header value is a wall-clock stamp, so the digits are
    # pinned the way _norm pins a numeric field — the *shape* is the contract.
    userinfo = re.sub(r"expire=\d+", "expire=<TS>",
                      r.headers.get("subscription-userinfo") or "")
    _check("public_sub_headers", r.status_code, {
        "userinfo": userinfo,
        "interval": r.headers.get("profile-update-interval"),
        "cacheControl": r.headers.get("cache-control"),
        "hasTitle": bool(r.headers.get("profile-title")),
    })
    r = await c.get(f"/sub/{'x' * 22}/info", headers={"accept": "*/*"})
    _check("public_sub_unknown", r.status_code, r.json())
    await c.aclose()
    await owner.aclose()
    await sara.aclose()


async def test_golden_errors(seeded):
    """The error envelope. `code` + `params` beside the English `detail` is what
    lets the UI translate; a route that loses its code silently un-translates."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    cases = [
        ("unknown_server", "get", "/api/keys?server=nope", None),
        ("unknown_key", "post", "/api/servers/s1/keys/999999/disable", None),
        ("unknown_package", "post", "/api/servers/s1/keys",
         {"name": "x", "package_id": 99999}),
        ("owner_only", "get", "/api/audit", None),
        ("bad_knob", "put", "/api/settings/panel", {"not_a_knob": 1}),
        ("knob_out_of_range", "put", "/api/settings/panel",
         {"notify_limit_percent": 900}),
    ]
    for name, method, url, body in cases:
        who = sara if name == "owner_only" else owner
        r = await getattr(who, method)(url, **({"json": body} if body else {}))
        _check(f"error_{name}", r.status_code, r.json())

    # the reseller cannot top herself up outside the price list (invariant M7)
    for name, url, body in [
        ("credit_limit", f"/api/servers/s1/keys/{ids['sara']}/limit", {"limit_gb": 500}),
        ("credit_monthly", f"/api/servers/s1/keys/{ids['sara']}/monthly", {"monthly_gb": 500}),
    ]:
        r = await sara.put(url, json=body)
        _check(f"error_{name}", r.status_code, r.json())
    r = await sara.post(f"/api/servers/s1/keys/{ids['sara']}/reset")
    _check("error_credit_reset", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_writes(seeded):
    """The write responses. These are the ones the re-architecture rewrites, so
    they are the ones most worth pinning."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)

    r = await owner.post("/api/servers/s1/keys",
                         json={"name": "golden-new", "limit_gb": 25, "days": 45},
                         headers={"Idempotency-Key": "golden-write-1"})
    _check("write_create_key", r.status_code, r.json())
    new_id = r.json()["id"]

    # the same key again: the replay must be byte-identical, not merely similar
    again = await owner.post("/api/servers/s1/keys",
                             json={"name": "golden-new", "limit_gb": 25, "days": 45},
                             headers={"Idempotency-Key": "golden-write-1"})
    assert again.json() == r.json(), "an idempotent replay changed shape"
    assert again.headers.get("Idempotent-Replay") == "true"

    for name, method, url, body in [
        ("rename", "put", f"/api/servers/s1/keys/{new_id}/name", {"name": "renamed"}),
        ("limit", "put", f"/api/servers/s1/keys/{new_id}/limit", {"limit_gb": 30}),
        ("monthly", "put", f"/api/servers/s1/keys/{new_id}/monthly", {"monthly_gb": 5}),
        ("disable", "post", f"/api/servers/s1/keys/{new_id}/disable", None),
        ("enable", "post", f"/api/servers/s1/keys/{new_id}/enable", None),
        ("extend", "post", f"/api/servers/s1/keys/{new_id}/extend", {"days": 10}),
        ("reset", "post", f"/api/servers/s1/keys/{new_id}/reset", None),
        ("sub", "post", f"/api/servers/s1/keys/{new_id}/sub", None),
        ("rotate", "post", f"/api/servers/s1/keys/{new_id}/rotate", None),
    ]:
        r = await getattr(owner, method)(url, **({"json": body} if body else {}))
        _check(f"write_{name}", r.status_code, r.json())

    # subscription membership, both directions
    tok = ids["token"]
    r = await owner.delete(f"/api/sub/{tok}/servers/s2")
    _check("write_sub_remove_server", r.status_code, r.json())
    r = await owner.post(f"/api/sub/{tok}/servers/s2")
    _check("write_sub_add_server", r.status_code, r.json())

    r = await owner.post("/api/servers/s1/bulk-servers", json={
        "action": "add",
        "keys": [{"server_id": "s1", "key_id": ids["active"]},
                 {"server_id": "s1", "key_id": "does-not-exist"}]})
    _check("write_bulk_partial", r.status_code, r.json())

    r = await owner.put(f"/api/servers/s1/keys/{ids['unlimited']}/owner",
                        json={"admin_id": ids["sara_admin"]})
    _check("write_set_owner", r.status_code, r.json())

    r = await owner.post(f"/api/admins/{ids['sara_admin']}/credit",
                         json={"delta": 250_000, "note": "golden top-up"})
    _check("write_credit_topup", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_audit_shape(seeded):
    """The audit log records the writes above. Its shape is what the owner's
    audit screen reads, and O2 (no secrets) is asserted here rather than trusted."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    await owner.put("/api/settings/panel", json={"notify_limit_percent": 75})
    r = await owner.get("/api/audit?limit=5")
    body = r.json()
    # actions and statuses are the contract; ids and bodies vary with the seed
    _check("audit_actions", r.status_code,
           [{"action": e["action"], "status": e["status"], "actor": e["actor_name"]}
            for e in body["entries"]])
    blob = json.dumps(body)
    for secret in ("sara-pw", "reza-pw", "\"pw\""):
        assert secret not in blob, f"the audit log leaked {secret!r}"
    await owner.aclose()
    await sara.aclose()


# ------------------------------------------------------- the rest of the surface
async def test_golden_admin_and_catalogue_writes(seeded):
    """Creating and editing the things the owner administers.

    These were the last responses with no snapshot at all, which made them the
    ones a `response_model` could quietly thin without anything noticing.
    """
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)

    r = await owner.post("/api/admins", json={
        "username": "goldie", "password": "goldie-pw", "caps": ["keys.view"],
        "servers": ["s1"], "credit_enabled": True, "discount_pct": 5})
    _check("write_create_admin", r.status_code, r.json())
    aid = r.json()["id"]
    r = await owner.put(f"/api/admins/{aid}",
                        json={"caps": ["keys.view", "keys.edit"], "disabled": True})
    _check("write_edit_admin", r.status_code, r.json())
    r = await owner.delete(f"/api/admins/{aid}")
    _check("write_delete_admin", r.status_code, r.json())

    r = await owner.post("/api/packages",
                         json={"name": "Golden", "gb": 5, "days": 7, "price": 50_000})
    _check("write_create_package", r.status_code, r.json())
    pid = r.json()["id"]
    r = await owner.put(f"/api/packages/{pid}",
                        json={"name": "Golden II", "gb": 6, "days": 7, "price": 60_000})
    _check("write_edit_package", r.status_code, r.json())
    r = await owner.delete(f"/api/packages/{pid}")
    _check("write_delete_package", r.status_code, r.json())

    r = await owner.put("/api/servers/s2", json={"name": "Berlin II"})
    _check("write_rename_server", r.status_code, r.json())
    for name, url, body in [
        ("metrics", "/api/servers/s1/settings/metrics", {"enabled": True}),
        ("global_limit", "/api/servers/s1/settings/global-limit", {"limit_gb": 500}),
        ("server_name", "/api/servers/s1/settings/name", {"name": "Tokyo Edge"}),
    ]:
        r = await owner.put(url, json=body)
        _check(f"write_server_{name}", r.status_code, r.json())
    r = await owner.delete("/api/servers/s2")
    _check("write_delete_server", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_settings_writes(seeded):
    """Panel settings, the profile host, and the two-factor enrolment flow."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)

    r = await owner.put("/api/settings/panel", json={"notify_limit_percent": 75})
    _check("write_panel_knob", r.status_code, r.json())
    r = await owner.post("/api/settings/panel/reset")
    _check("write_panel_reset", r.status_code, r.json())
    r = await owner.put("/api/settings/profile",
                        json={"baseUrl": "https://star.example.com"})
    _check("write_profile_host", r.status_code, r.json())
    r = await owner.put("/api/settings/profile", json={"baseUrl": ""})
    _check("write_profile_host_cleared", r.status_code, r.json())

    # Enrolling a second factor. The secret and its otpauth:// URI are the
    # contract with every authenticator app, so the shape is worth pinning even
    # though the values are random by design.
    from outline_panel.core import security
    r = await owner.post("/api/me/2fa/start")
    _check("write_2fa_start", r.status_code, r.json())
    secret = r.json()["secret"]
    r = await owner.post("/api/me/2fa/enable", json={"code": security.totp_now(secret)})
    _check("write_2fa_enable", r.status_code, r.json())
    r = await owner.get("/api/me/2fa")
    _check("read_2fa_on", r.status_code, r.json())
    r = await owner.post("/api/me/2fa/disable", json={"password": "pw"})
    _check("write_2fa_disable", r.status_code, r.json())

    r = await owner.post("/api/me/password", json={"current": "pw", "new": "pw"})
    _check("write_my_password", r.status_code, r.json())
    r = await owner.post("/api/settings/password", json={"current": "pw", "new": "pw"})
    _check("write_owner_password", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_convergence_and_snapshots(seeded):
    """The operator's two windows into state: what has not reached a server yet,
    and what is on disk."""
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    for name, url in [("pending", "/api/convergence"),
                      ("drift", "/api/convergence/drift")]:
        r = await owner.get(url)
        _check(f"read_convergence_{name}", r.status_code, r.json())
    r = await owner.post("/api/convergence/drift")
    _check("write_convergence_apply", r.status_code, r.json())
    r = await owner.post("/api/snapshots")
    _check("write_snapshot", r.status_code, r.json())
    r = await owner.delete(f"/api/snapshots/{r.json()['name']}")
    _check("write_snapshot_delete", r.status_code, r.json())
    await owner.aclose()
    await sara.aclose()


async def test_golden_mini_app(seeded):
    """The Telegram Mini App.

    A separate client with its own authentication, calling the same use cases
    through different routes — so its responses are their own contract, and they
    had no snapshot at all. `initData` is forged here the way Telegram signs it.
    """
    import hashlib
    import hmac
    import json as _json
    import time as _time
    application, deps, fakes = seeded
    owner, sara, ids = await _seed(seeded)
    token = "123456:golden-bot-token"
    await deps.settings.set("bot_token", token)

    def init_data(uid: int) -> str:
        fields = {"auth_date": str(int(_time.time())),
                  "user": _json.dumps({"id": uid, "first_name": "Sara"})}
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        sig = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return "&".join([*(f"{k}={fields[k]}" for k in sorted(fields)), f"hash={sig}"])

    tma = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                            base_url="http://panel.example.com",
                            headers={"Authorization": "tma " + init_data(111)})
    for name, url in [("bootstrap", "/tma/api/bootstrap"), ("keys", "/tma/api/keys"),
                      ("stats", "/tma/api/stats"), ("packages", "/tma/api/packages")]:
        r = await tma.get(url)
        _check(f"tma_{name}", r.status_code, r.json())

    kid = ids["sara"]
    r = await tma.put(f"/tma/api/keys/s1/{kid}/name", json={"name": "renamed-in-telegram"})
    _check("tma_rename", r.status_code, r.json())
    r = await tma.post(f"/tma/api/keys/s1/{kid}/disable")
    _check("tma_disable", r.status_code, r.json())
    r = await tma.post(f"/tma/api/keys/s1/{kid}/enable")
    _check("tma_enable", r.status_code, r.json())
    r = await tma.post(f"/tma/api/keys/s1/{kid}/sub")
    _check("tma_sub", r.status_code, r.json())
    r = await tma.delete(f"/tma/api/keys/s1/{kid}")
    _check("tma_delete", r.status_code, r.json())

    # and the surface an unsigned blob must not reach
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://panel.example.com",
                             headers={"Authorization": "tma hash=forged"})
    r = await anon.get("/tma/api/keys")
    _check("tma_forged_auth", r.status_code, r.json())
    await anon.aclose()
    await tma.aclose()
    await owner.aclose()
    await sara.aclose()
