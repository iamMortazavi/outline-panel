"""Two-factor belongs to an account, not to the panel.

It used to live in `settings` — one global row — so it guarded the owner, who
signs in rarely, and left every reseller who signs in daily on a password alone.
"""
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_subadmins import _login, _mk_sub, app  # noqa: F401  (fixtures)


def _code(secret, offset=0):

    from outline_panel.core import security
    # the panel's own generator, so the test cannot drift from the verifier
    return security.totp_now(secret) if hasattr(security, "totp_now") else _hotp(secret, offset)


def _hotp(secret, offset=0):
    import base64
    import hashlib
    import hmac
    import struct
    import time as _t
    key = base64.b32decode(secret, casefold=True)
    counter = int(_t.time()) // 30 + offset
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6:06d}"


async def _client(application):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://x")


async def _enroll(c):
    """Turn 2FA on for whoever `c` is signed in as. Returns the secret."""
    start = await c.post("/api/me/2fa/start")
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    assert start.json()["uri"].startswith("otpauth://")
    on = await c.post("/api/me/2fa/enable", json={"code": _hotp(secret)})
    assert on.status_code == 200, on.text
    return secret


async def test_a_subadmin_can_turn_on_their_own_second_factor(app):
    application, deps, _ = app
    await _mk_sub(deps, "sara", caps="keys.view")
    sara = await _login(application, "sara", "sara-pw")
    assert (await sara.get("/api/me/2fa")).json() == {"enabled": False, "pending": False}

    secret = await _enroll(sara)
    assert (await sara.get("/api/me/2fa")).json()["enabled"] is True
    await sara.aclose()

    # from now on her password alone is not a login
    fresh = await _client(application)
    r = await fresh.post("/api/login", json={"username": "sara", "password": "sara-pw"})
    assert r.status_code == 401
    assert r.json()["code"] == "auth.totp_required"

    r = await fresh.post("/api/login", json={"username": "sara", "password": "sara-pw",
                                             "totp": "000000"})
    assert r.status_code == 401 and r.json()["code"] == "auth.totp_invalid"

    r = await fresh.post("/api/login", json={"username": "sara", "password": "sara-pw",
                                             "totp": _hotp(secret)})
    assert r.status_code == 200, r.text
    await fresh.aclose()


async def test_the_code_must_be_proved_before_it_is_switched_on(app):
    """A misread QR must not lock someone out of their own panel."""
    application, deps, _ = app
    await _mk_sub(deps, "sara", caps="keys.view")
    sara = await _login(application, "sara", "sara-pw")
    await sara.post("/api/me/2fa/start")
    bad = await sara.post("/api/me/2fa/enable", json={"code": "000000"})
    assert bad.status_code == 401 and bad.json()["code"] == "auth.totp_invalid"
    assert (await sara.get("/api/me/2fa")).json() == {"enabled": False, "pending": True}
    await sara.aclose()

    # and she can still get in with just her password
    fresh = await _client(application)
    assert (await fresh.post("/api/login",
                             json={"username": "sara", "password": "sara-pw"})).status_code == 200
    await fresh.aclose()


async def test_one_admins_factor_is_not_another_admins(app):
    application, deps, _ = app
    await _mk_sub(deps, "sara", caps="keys.view")
    await _mk_sub(deps, "reza", caps="keys.view")
    sara = await _login(application, "sara", "sara-pw")
    await _enroll(sara)
    await sara.aclose()

    fresh = await _client(application)
    # reza never enrolled; his login is untouched
    assert (await fresh.post("/api/login",
                             json={"username": "reza", "password": "sara-pw"})).status_code == 200
    # and neither is the owner's
    assert (await fresh.post("/api/login",
                             json={"username": "admin", "password": "pw"})).status_code == 200
    await fresh.aclose()


async def test_turning_it_off_costs_a_password(app):
    """A borrowed open tab must not be able to strip the factor."""
    application, deps, _ = app
    await _mk_sub(deps, "sara", caps="keys.view")
    sara = await _login(application, "sara", "sara-pw")
    await _enroll(sara)

    bad = await sara.post("/api/me/2fa/disable", json={"password": "not-it"})
    assert bad.status_code == 401
    assert (await sara.get("/api/me/2fa")).json()["enabled"] is True

    ok = await sara.post("/api/me/2fa/disable", json={"password": "sara-pw"})
    assert ok.status_code == 200
    assert (await sara.get("/api/me/2fa")).json() == {"enabled": False, "pending": False}
    await sara.aclose()

    fresh = await _client(application)
    assert (await fresh.post("/api/login",
                             json={"username": "sara", "password": "sara-pw"})).status_code == 200
    await fresh.aclose()


async def test_the_owner_still_enrolls_through_their_own_screen(app):
    """The owner-only routes stay, and must not disagree with the new ones."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    start = await owner.post("/api/settings/2fa/start")
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    assert (await owner.post("/api/settings/2fa/enable",
                             json={"code": _hotp(secret)})).status_code == 200

    # both views agree
    assert (await owner.get("/api/me/2fa")).json()["enabled"] is True
    assert (await owner.get("/api/settings")).json()["totpEnabled"] is True

    fresh = await _client(application)
    r = await fresh.post("/api/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 401 and r.json()["code"] == "auth.totp_required"
    assert (await fresh.post("/api/login", json={"username": "admin", "password": "pw",
                                                 "totp": _hotp(secret)})).status_code == 200
    await fresh.aclose()
    await owner.aclose()


async def test_an_owner_secret_from_before_the_migration_still_works(app):
    """Upgrading must not drop the factor the owner already had."""
    application, deps, _ = app
    from outline_panel.core import security
    from outline_panel.core.settings import TOTP_ENABLED, TOTP_SECRET
    from outline_panel.web import deps as d

    secret = security.generate_totp_secret()
    await d.settings.set(TOTP_SECRET, secret)          # the old global location
    await d.settings.set_bool(TOTP_ENABLED, True)
    owner = await d.db.get_owner()
    await d.db.update_admin(owner["id"], totp_secret=None, totp_enabled=0)

    fresh = await _client(application)
    r = await fresh.post("/api/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 401 and r.json()["code"] == "auth.totp_required"
    assert (await fresh.post("/api/login", json={"username": "admin", "password": "pw",
                                                 "totp": _hotp(secret)})).status_code == 200
    await fresh.aclose()
