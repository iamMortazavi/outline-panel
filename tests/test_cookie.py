import os
import sys
import tempfile

import httpx
import pytest


async def _make_app(trust_proxy: bool):
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ.pop("COOKIE_SECURE", None)        # default -> "auto"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    os.environ["TRUST_PROXY"] = "true" if trust_proxy else "false"
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    return appmod.app, deps


@pytest.fixture
async def app():
    app, deps = await _make_app(trust_proxy=False)
    yield app
    await deps.db.close()


@pytest.fixture
async def proxied_app():
    app, deps = await _make_app(trust_proxy=True)
    yield app
    await deps.db.close()


async def test_auto_cookie_not_secure_over_http(app):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        r = await c.post("/api/login", json={"password": "pw"})
        cookie = r.headers.get("set-cookie", "").lower()
        assert "secure" not in cookie          # http -> not Secure, so it sticks
        assert (await c.get("/api/me")).status_code == 200   # stays logged in


async def test_forwarded_proto_ignored_without_trust_proxy(app):
    # A spoofable header must NOT flip the cookie to Secure when not behind a
    # trusted proxy — otherwise an attacker could lock the admin out over HTTP.
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        r = await c.post("/api/login", json={"password": "pw"},
                         headers={"x-forwarded-proto": "https"})
        assert "secure" not in r.headers.get("set-cookie", "").lower()


async def test_auto_cookie_secure_behind_trusted_proxy(proxied_app):
    t = httpx.ASGITransport(app=proxied_app)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        r = await c.post("/api/login", json={"password": "pw"},
                         headers={"x-forwarded-proto": "https"})
        assert "secure" in r.headers.get("set-cookie", "").lower()
