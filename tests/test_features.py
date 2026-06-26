import base64
import os
import sys
import tempfile

import httpx
import pytest

GB = 1024 ** 3


class FakeOutline:
    def __init__(self):
        self.keys = {}
        self.usage = {}
        self.limits = {}
        self._id = 0

    async def create_key(self, name=None, limit_bytes=None):
        self._id += 1
        kid = str(self._id)
        self.keys[kid] = {"id": kid, "name": name,
                          "accessUrl": f"ss://YWVzOnB3@1.2.3.4:8388/?o=1",
                          "dataLimit": {"bytes": limit_bytes} if limit_bytes else {}}
        if limit_bytes:
            self.limits[kid] = limit_bytes
        return self.keys[kid]

    async def get_key(self, kid):
        return self.keys[kid]

    async def delete_key(self, kid):
        self.keys.pop(kid, None)

    async def list_keys(self):
        return list(self.keys.values())

    async def get_transfer_metrics(self):
        return self.usage

    async def set_data_limit(self, kid, b):
        self.limits[kid] = b

    async def remove_data_limit(self, kid):
        self.limits.pop(kid, None)

    async def get_server_metrics(self, since="30d"):
        from outline_panel.outline_api import OutlineError
        raise OutlineError("off")

    async def get_server_info(self):
        return {"name": "fake", "version": "1.0"}

    async def close(self):
        pass


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
    fake = FakeOutline()
    deps.reg.servers["s1"] = {"id": "s1", "name": "S1", "api_url": "https://1.2.3.4:1/x",
                              "cert_sha256": None, "api": fake}
    await deps.db.add_server("s1", "S1", "https://1.2.3.4:1/x")
    yield appmod.app, deps, fake
    await deps.db.close()


async def _client(application):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    await c.post("/api/login", json={"password": "pw"})
    return c


async def test_reset_usage(app):
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 10, "days": 0})).json()["id"]
    fake.usage[kid] = 3 * GB
    r = await c.post(f"/api/servers/s1/keys/{kid}/reset")
    assert r.status_code == 200
    assert r.json()["limit"] == 13 * GB          # used(3) + allowance(10)
    assert fake.limits[kid] == 13 * GB
    await c.aclose()


async def test_subscription_link(app):
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "Bob", "limit_gb": 0, "days": 0})).json()["id"]
    sub = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()
    assert sub["path"].startswith("/sub/")
    # public fetch, no auth
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as pub:
        r = await pub.get(sub["path"])
        assert r.status_code == 200
        decoded = base64.b64decode(r.text).decode()
        assert decoded.startswith("ss://") and "#Bob" in decoded
    await c.aclose()


async def test_subscription_unknown_token(app):
    application, deps, fake = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as pub:
        assert (await pub.get("/sub/nope")).status_code == 404


async def test_backup_and_restore(app):
    application, deps, fake = app
    c = await _client(application)
    await c.post("/api/servers/s1/keys", json={"name": "A", "limit_gb": 1, "days": 0})
    backup = (await c.get("/api/backup")).json()
    assert backup["servers"] and backup["keys"] and "settings" in backup

    # wipe to a different shape then restore
    await deps.db.delete_key("s1", backup["keys"][0]["key_id"])
    assert await deps.db.all_keys() == []
    r = await c.post("/api/restore", json=backup)
    assert r.status_code == 200 and r.json()["keys"] == 1
    assert len(await deps.db.all_keys()) == 1
    await c.aclose()


async def test_restore_rejects_garbage(app):
    application, deps, fake = app
    c = await _client(application)
    assert (await c.post("/api/restore", json={"nope": 1})).status_code == 400
    await c.aclose()
