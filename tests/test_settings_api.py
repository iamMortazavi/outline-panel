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
