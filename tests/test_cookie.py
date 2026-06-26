import os
import sys
import tempfile

import httpx
import pytest


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ.pop("COOKIE_SECURE", None)        # default -> "auto"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    yield appmod.app
    await deps.db.close()


async def test_auto_cookie_not_secure_over_http(app):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        r = await c.post("/api/login", json={"password": "pw"})
        cookie = r.headers.get("set-cookie", "").lower()
        assert "secure" not in cookie          # http -> not Secure, so it sticks
        assert (await c.get("/api/me")).status_code == 200   # stays logged in


async def test_auto_cookie_secure_behind_https_proxy(app):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        r = await c.post("/api/login", json={"password": "pw"},
                         headers={"x-forwarded-proto": "https"})
        assert "secure" in r.headers.get("set-cookie", "").lower()
