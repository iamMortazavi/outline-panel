import os
import sys
import tempfile

import httpx
import pytest


class FakeOutline:
    """Minimal stand-in for OutlineAPI used by the web app."""

    def __init__(self):
        self.keys = []
        self._id = 0

    async def create_key(self, name=None, limit_bytes=None):
        self._id += 1
        kid = str(self._id)
        dl = {"bytes": limit_bytes} if limit_bytes else {}
        self.keys.append({"id": kid, "name": name, "dataLimit": dl})
        return {"id": kid, "accessUrl": f"ss://abc@1.2.3.4:8388/#{name or ''}"}

    async def delete_key(self, kid):
        self.keys = [k for k in self.keys if k["id"] != kid]

    async def get_key(self, kid):
        for k in self.keys:
            if k["id"] == kid:
                return {"id": kid, "name": k.get("name"),
                        "accessUrl": f"ss://abc@1.2.3.4:8388/?outline=1#{k.get('name') or ''}"}
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("no such key")

    async def list_keys(self):
        return self.keys

    async def get_transfer_metrics(self):
        return {}

    async def get_server_metrics(self, since="30d"):
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("metrics off")

    async def get_server_metrics_cached(self, since="30d", ttl=15.0):
        return await self.get_server_metrics(since)

    async def get_server_info(self):
        return {"name": "fake", "version": "1.0"}

    async def close(self):
        pass


class BrokenOutline(FakeOutline):
    async def list_keys(self):
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("server down")


class DeadOutline(FakeOutline):
    """Works until ``dead`` is set, then every call fails — a server that went
    down after its keys were created."""

    dead = False

    async def get_key(self, kid):
        if self.dead:
            from outline_panel.core.outline_api import OutlineError
            raise OutlineError("server down")
        return await super().get_key(kid)

    async def get_transfer_metrics(self):
        if self.dead:
            from outline_panel.core.outline_api import OutlineError
            raise OutlineError("server down")
        return await super().get_transfer_metrics()


class App:
    """Bundle of the freshly-loaded app module + its deps for a test."""

    def __init__(self, app, deps):
        self.app = app
        self.deps = deps
        self.db = deps.db
        self.reg = deps.reg


@pytest.fixture
async def app(monkeypatch):
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["TRUST_PROXY"] = "false"   # hermetic: ignore forwarded headers
    os.environ.pop("OUTLINE_API_URL", None)
    # fresh import so deps.db points at this test's temp DB
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    yield App(appmod.app, deps)
    await deps.db.close()


def _register(a, sid, name, api):
    a.reg.servers[sid] = {
        "id": sid, "name": name, "api_url": "https://1.2.3.4:1/x",
        "cert_sha256": None, "api": api,
    }


async def _client(a, login=True):
    transport = httpx.ASGITransport(app=a.app)
    c = httpx.AsyncClient(transport=transport, base_url="http://t")
    if login:
        await c.post("/api/login", json={"password": "pw"})
    return c


async def test_auth_required(app):
    c = await _client(app, login=False)
    r = await c.get("/api/me")
    assert r.status_code == 401
    await c.aclose()


async def test_login_wrong_then_right(app):
    c = await _client(app, login=False)
    assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401
    r = await c.post("/api/login", json={"password": "pw"})
    assert r.status_code == 200
    assert "secure" not in r.headers.get("set-cookie", "").lower()  # COOKIE_SECURE=false
    assert (await c.get("/api/me")).status_code == 200
    await c.aclose()


async def test_login_rate_limit(app):
    c = await _client(app, login=False)
    codes = [
        (await c.post("/api/login", json={"password": "x"},
                      headers={"x-forwarded-for": "5.5.5.5"})).status_code
        for _ in range(7)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    await c.aclose()


async def test_rate_limit_not_bypassed_by_rotating_xff(app):
    # Without TRUST_PROXY (default), X-Forwarded-For is ignored, so rotating it
    # per request must NOT mint a fresh rate-limit bucket each time.
    c = await _client(app, login=False)
    codes = [
        (await c.post("/api/login", json={"password": "x"},
                      headers={"x-forwarded-for": f"9.9.9.{i}"})).status_code
        for i in range(7)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    await c.aclose()


async def test_healthz_no_auth(app):
    c = await _client(app, login=False)
    r = await c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    await c.aclose()


async def test_keys_surface_unreachable_server(app):
    _register(app, "bad", "Broken", BrokenOutline())
    await app.db.add_server("bad", "Broken", "https://1.2.3.4:1/x")
    c = await _client(app)
    j = (await c.get("/api/keys")).json()
    assert j["keys"] == []
    assert [e["serverName"] for e in j["errors"]] == ["Broken"]
    await c.aclose()


async def test_create_key_with_monthly_quota(app):
    fake = FakeOutline()
    _register(app, "s1", "S1", fake)
    await app.db.add_server("s1", "S1", "https://1.2.3.4:1/x")
    c = await _client(app)
    r = await c.post("/api/servers/s1/keys",
                     json={"name": "Ali", "limit_gb": 0, "days": 0, "monthly_gb": 5})
    assert r.status_code == 200
    kid = r.json()["id"]
    meta = await app.db.get_key("s1", kid)
    assert meta["monthly_bytes"] == 5 * 1024 ** 3
    assert meta["reset_ts"] is not None
    # monthly with no explicit limit => limit seeded to the monthly allowance
    assert meta["limit_bytes"] == 5 * 1024 ** 3
    await c.aclose()


async def test_subscription_link_and_headers(app):
    import base64
    fake = FakeOutline()
    _register(app, "s1", "Tokyo", fake)
    await app.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    c = await _client(app)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Ali", "limit_gb": 10, "days": 30})).json()["id"]
    r = await c.post(f"/api/servers/s1/keys/{kid}/sub")
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["servers"][0]["included"] is True
    pub = await _client(app, login=False)            # public, no auth
    sub = await pub.get(f"/sub/{token}")
    assert sub.status_code == 200
    assert "download=" in sub.headers["subscription-userinfo"]
    assert "total=" in sub.headers["subscription-userinfo"]
    assert sub.headers["profile-update-interval"] == "12"
    body = base64.b64decode(sub.text).decode()
    assert body.startswith("ss://") and "?outline" not in body  # path stripped
    await pub.aclose()
    await c.aclose()


async def test_subscription_multi_server(app):
    import base64
    f1, f2 = FakeOutline(), FakeOutline()
    _register(app, "s1", "Tokyo", f1)
    _register(app, "s2", "Berlin", f2)
    await app.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    await app.db.add_server("s2", "Berlin", "https://1.2.3.4:1/x")
    c = await _client(app)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Vip", "limit_gb": 50, "days": 0})).json()["id"]
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    r = await c.post(f"/api/sub/{token}/servers/s2")          # mirror onto s2
    assert r.status_code == 200 and len(r.json()["members"]) == 2
    pub = await _client(app, login=False)
    body = base64.b64decode((await pub.get(f"/sub/{token}")).text).decode()
    assert len(body.splitlines()) == 2                        # two configs
    r2 = await c.delete(f"/api/sub/{token}/servers/s2")       # unlink s2
    assert len(r2.json()["members"]) == 1
    body2 = base64.b64decode((await pub.get(f"/sub/{token}")).text).decode()
    assert len(body2.splitlines()) == 1
    await pub.aclose()
    await c.aclose()


async def test_subscription_browser_vs_client(app):
    fake = FakeOutline()
    _register(app, "s1", "Tokyo", fake)
    await app.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    c = await _client(app)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Ali", "limit_gb": 10, "days": 0})).json()["id"]
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    pub = await _client(app, login=False)
    # a browser gets the human page
    page = await pub.get(f"/sub/{token}", headers={
        "Accept": "text/html", "User-Agent": "Mozilla/5.0"})
    assert page.status_code == 200 and "/static/vendor/qrcode.js" in page.text
    # a VPN client gets the raw base64 sub — what it actually sends
    raw = await pub.get(f"/sub/{token}", headers={
        "Accept": "*/*", "User-Agent": "v2rayNG/1.8"})
    assert raw.status_code == 200 and "subscription-userinfo" in raw.headers
    # ...and still does if it asks for html without claiming to be a browser
    raw2 = await pub.get(f"/sub/{token}", headers={
        "Accept": "text/html", "User-Agent": "v2rayNG/1.8"})
    assert raw2.status_code == 200 and "subscription-userinfo" in raw2.headers
    # ?format=raw is the escape hatch for a client that spoofs a browser
    esc = await pub.get(f"/sub/{token}?format=raw", headers={
        "Accept": "text/html", "User-Agent": "Mozilla/5.0"})
    assert esc.status_code == 200 and "subscription-userinfo" in esc.headers
    # the JSON usage summary that powers the page — never cached, it carries the keys
    r = await pub.get(f"/sub/{token}/info")
    assert r.headers["cache-control"] == "no-store"
    info = r.json()
    assert info["name"] == "Ali" and info["unlimited"] is False
    assert info["total"] == 10 * 1024 ** 3 and len(info["servers"]) == 1
    await pub.aclose()
    await c.aclose()


async def test_subscription_all_servers_down(app):
    """An unresolvable subscription must fail, not render as an empty plan:
    ``total==0`` is how the page spells "unlimited"."""
    fake = DeadOutline()
    _register(app, "s1", "Tokyo", fake)
    await app.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    c = await _client(app)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Ali", "limit_gb": 10, "days": 0})).json()["id"]
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    fake.dead = True

    pub = await _client(app, login=False)
    raw = await pub.get(f"/sub/{token}", headers={"Accept": "*/*", "User-Agent": "v2rayNG"})
    assert raw.status_code == 502
    assert (await pub.get(f"/sub/{token}/info")).status_code == 502
    await pub.aclose()
    await c.aclose()


async def test_extend_reduce_and_zero(app):
    fake = FakeOutline()
    _register(app, "s1", "S1", fake)
    await app.db.add_server("s1", "S1", "https://1.2.3.4:1/x")
    c = await _client(app)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "X", "limit_gb": 0, "days": 30})).json()["id"]
    # reduce a pending key's duration 30 -> 20
    assert (await c.post(f"/api/servers/s1/keys/{kid}/extend",
                         json={"days": -10})).status_code == 200
    assert (await app.db.get_key("s1", kid))["duration_days"] == 20
    # extend +5 -> 25
    await c.post(f"/api/servers/s1/keys/{kid}/extend", json={"days": 5})
    assert (await app.db.get_key("s1", kid))["duration_days"] == 25
    # zero is rejected
    assert (await c.post(f"/api/servers/s1/keys/{kid}/extend",
                         json={"days": 0})).status_code == 400
    await c.aclose()
