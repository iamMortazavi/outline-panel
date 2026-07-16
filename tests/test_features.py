import base64
import os
import sys
import tempfile
import time

import httpx
import pytest

GB = 1024 ** 3


class FakeOutline:
    def __init__(self):
        self.keys = {}
        self.usage = {}
        self.limits = {}
        self._id = 0
        self.fail_limit_writes = False  # simulate a briefly unreachable server

    async def create_key(self, name=None, limit_bytes=None):
        self._id += 1
        kid = str(self._id)
        self.keys[kid] = {"id": kid, "name": name,
                          "accessUrl": "ss://YWVzOnB3@1.2.3.4:8388/?o=1",
                          "dataLimit": {"bytes": limit_bytes} if limit_bytes else {}}
        if limit_bytes:
            self.limits[kid] = limit_bytes
        return self.keys[kid]

    async def get_key(self, kid):
        return self.keys[kid]

    async def delete_key(self, kid):
        if kid not in self.keys:  # the real API 404s on a key that's already gone
            from outline_panel.core.outline_api import OutlineError
            raise OutlineError("Error response from server (404): Not Found",
                               status=404)
        self.keys.pop(kid, None)

    async def list_keys(self):
        return list(self.keys.values())

    async def get_transfer_metrics(self):
        return self.usage

    def _check(self):
        if self.fail_limit_writes:
            from outline_panel.core.outline_api import OutlineError
            raise OutlineError("server down")

    async def set_data_limit(self, kid, b):
        self._check()
        self.limits[kid] = b

    async def remove_data_limit(self, kid):
        self._check()
        self.limits.pop(kid, None)

    async def get_server_metrics(self, since="30d"):
        from outline_panel.core.outline_api import OutlineError
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
                        json={"name": "A", "limit_gb": 10, "days": 0,
                              "monthly_gb": 10})).json()["id"]
    fake.usage[kid] = 3 * GB
    r = await c.post(f"/api/servers/s1/keys/{kid}/reset")
    assert r.status_code == 200
    assert r.json()["limit"] == 13 * GB          # used(3) + allowance(10)
    assert fake.limits[kid] == 13 * GB
    await c.aclose()


async def test_reset_needs_a_monthly_quota(app):
    """limit_bytes is the cumulative ceiling /reset itself writes, so it cannot
    double as the per-cycle size — reusing it compounded (10->20->40 GB)."""
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 10, "days": 0})).json()["id"]
    r = await c.post(f"/api/servers/s1/keys/{kid}/reset")
    assert r.status_code == 400 and "monthly" in r.json()["detail"].lower()

    # with a quota it works, and every cycle grants the same allowance
    await c.put(f"/api/servers/s1/keys/{kid}/monthly", json={"monthly_gb": 10})
    for _ in range(3):
        fake.usage[kid] = int((await deps.db.get_key("s1", kid))["limit_bytes"])
        new = (await c.post(f"/api/servers/s1/keys/{kid}/reset")).json()["limit"]
        assert new - fake.usage[kid] == 10 * GB
    await c.aclose()


async def test_monthly_quota_reaches_outline(app):
    """Saving a quota on an existing key must meter it now, not a cycle later."""
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 0, "days": 0})).json()["id"]
    fake.usage[kid] = 2 * GB
    assert fake.limits.get(kid) is None  # unlimited today
    r = await c.put(f"/api/servers/s1/keys/{kid}/monthly", json={"monthly_gb": 5})
    assert r.status_code == 200
    assert fake.limits[kid] == 7 * GB     # used(2) + quota(5)
    await c.aclose()


async def test_extend_does_not_commit_before_outline(app):
    """A 502 must not move the expiry, or the admin's retry extends twice."""
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 10, "days": 30})).json()["id"]
    now = int(time.time())
    await deps.db.activate("s1", kid, now - 30 * 86400, now - 86400)
    await deps.db.set_disabled("s1", kid, True)

    fake.fail_limit_writes = True
    r = await c.post(f"/api/servers/s1/keys/{kid}/extend", json={"days": 30})
    assert r.status_code == 502
    assert (await deps.db.get_key("s1", kid))["expiry_ts"] == now - 86400  # untouched

    fake.fail_limit_writes = False
    assert (await c.post(f"/api/servers/s1/keys/{kid}/extend",
                         json={"days": 30})).status_code == 200
    meta = await deps.db.get_key("s1", kid)
    assert round((meta["expiry_ts"] - now) / 86400) == 30  # one extension, not two
    assert meta["disabled"] == 0
    await c.aclose()


async def test_delete_key_already_gone_from_outline(app):
    """A key deleted straight from Outline Manager must not leave a ghost row:
    invisible in the key list, yet still holding the subscription token."""
    application, deps, fake = app
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 10, "days": 0})).json()["id"]
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]
    fake.keys.clear()  # vanished upstream

    assert (await c.delete(f"/api/servers/s1/keys/{kid}")).status_code == 200
    assert await deps.db.get_key("s1", kid) is None
    assert await deps.db.get_keys_by_sub_token(token) == []
    await c.aclose()


async def test_sub_mirror_inherits_suspension(app):
    """The mirror is the same subscription — an expired, suspended user must not
    get a live config with a fresh clock on the second server."""
    application, deps, fake = app
    f2 = FakeOutline()
    deps.reg.servers["s2"] = {"id": "s2", "name": "S2", "api_url": "https://5.6.7.8:1/x",
                              "cert_sha256": None, "api": f2}
    await deps.db.add_server("s2", "S2", "https://5.6.7.8:1/x")
    c = await _client(application)
    kid = (await c.post("/api/servers/s1/keys",
                        json={"name": "A", "limit_gb": 10, "days": 30})).json()["id"]
    now = int(time.time())
    await deps.db.activate("s1", kid, now - 25 * 86400, now - 86400)  # expired
    await deps.db.set_disabled("s1", kid, True)
    token = (await c.post(f"/api/servers/s1/keys/{kid}/sub")).json()["token"]

    assert (await c.post(f"/api/sub/{token}/servers/s2")).status_code == 200
    mirror = [m for m in await deps.db.get_keys_by_sub_token(token)
              if m["server_id"] == "s2"][0]
    assert mirror["disabled"] == 1
    assert mirror["expiry_ts"] == now - 86400            # inherits, not restarts
    assert mirror["activated_ts"] == now - 25 * 86400
    assert f2.limits[mirror["key_id"]] == 0              # suspended on Outline too
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
