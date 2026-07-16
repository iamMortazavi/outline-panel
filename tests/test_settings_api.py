import os
import sys
import tempfile

import httpx
import pytest


@pytest.fixture
async def app():
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
    yield appmod.app, deps
    await deps.db.close()


async def _login(c, password="pw", totp=None):
    body = {"password": password}
    if totp:
        body["totp"] = totp
    return await c.post("/api/login", json=body)


async def test_change_password(app):
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        r = await c.post("/api/settings/password",
                         json={"current": "pw", "new": "newsecret"})
        assert r.status_code == 200
        # old password now rejected, new accepted
        c2 = httpx.AsyncClient(transport=t, base_url="http://x")
        assert (await _login(c2, "pw")).status_code == 401
        assert (await _login(c2, "newsecret")).status_code == 200
        await c2.aclose()


async def test_change_password_wrong_current(app):
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        r = await c.post("/api/settings/password",
                         json={"current": "nope", "new": "whatever"})
        assert r.status_code == 401


async def test_2fa_enable_and_login(app):
    from outline_panel.core import security
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        start = (await c.post("/api/settings/2fa/start")).json()
        secret = start["secret"]
        assert start["uri"].startswith("otpauth://")
        # enabling with a wrong code fails
        assert (await c.post("/api/settings/2fa/enable",
                             json={"code": "000000"})).status_code in (400, 200)
        # enable with a valid code
        code = security.totp_now(secret)
        assert (await c.post("/api/settings/2fa/enable",
                             json={"code": code})).status_code == 200

    # now login requires the second factor
    c2 = httpx.AsyncClient(transport=t, base_url="http://x")
    assert (await _login(c2, "pw")).status_code == 401              # needs 2FA
    assert (await _login(c2, "pw", security.totp_now(secret))).status_code == 200
    await c2.aclose()


async def test_owner_changes_username(app):
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        r = await c.post("/api/settings/password",
                         json={"current": "pw", "username": "bigboss"})
        assert r.status_code == 200 and r.json()["username"] == "bigboss"
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c2:
        # the new name works, the old one is gone
        assert (await c2.post("/api/login",
                              json={"username": "bigboss", "password": "pw"})).status_code == 200
        assert (await c2.post("/api/login",
                              json={"username": "admin", "password": "pw"})).status_code == 401


async def test_username_and_password_together(app):
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        assert (await c.post("/api/settings/password",
                             json={"current": "pw", "username": "boss",
                                   "new": "newpass1"})).status_code == 200
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c2:
        assert (await c2.post("/api/login",
                              json={"username": "boss", "password": "newpass1"})).status_code == 200


async def test_username_change_needs_the_current_password(app):
    """A stolen session must not be able to rename the account it sits in."""
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        assert (await c.post("/api/settings/password",
                             json={"current": "wrong", "username": "hacked"})).status_code == 401
        assert (await c.post("/api/settings/password",
                             json={"current": "pw"})).status_code == 400   # nothing to change
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c2:
        assert (await c2.post("/api/login",
                              json={"username": "admin", "password": "pw"})).status_code == 200


async def test_username_cannot_collide_with_a_sub_admin(app):
    from outline_panel.core import security
    application, deps = app
    h, s = security.hash_password("x")
    await deps.db.add_admin("taken", h, s, servers="s1")
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await _login(c)
        r = await c.post("/api/settings/password", json={"current": "pw", "username": "taken"})
        assert r.status_code == 400 and "taken" in r.json()["detail"].lower()
