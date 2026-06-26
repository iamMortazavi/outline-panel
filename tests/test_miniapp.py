import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from urllib.parse import urlencode

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from outline_panel.core import security  # noqa: E402

TOKEN = "123456:TEST-bot-token-abcdef"


def make_init_data(token=TOKEN, user=None, auth_date=None, tamper=False):
    user = user or {"id": 777, "first_name": "Ali"}
    data = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAterf",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        data["user"] = json.dumps({"id": 999, "first_name": "Eve"}, separators=(",", ":"))
    return urlencode(data)


# ---------------------------------------------------------- pure verification
def test_verify_valid():
    out = security.verify_telegram_init_data(make_init_data(), TOKEN)
    assert out["user"]["id"] == 777


def test_verify_bad_signature():
    with pytest.raises(ValueError):
        security.verify_telegram_init_data(make_init_data(tamper=True), TOKEN)


def test_verify_wrong_token():
    with pytest.raises(ValueError):
        security.verify_telegram_init_data(make_init_data(), "999:other")


def test_verify_expired():
    old = int(time.time()) - 10 * 86400
    with pytest.raises(ValueError):
        security.verify_telegram_init_data(make_init_data(auth_date=old), TOKEN)


def test_verify_no_hash():
    with pytest.raises(ValueError):
        security.verify_telegram_init_data("auth_date=1&user=%7B%7D", TOKEN)


# ------------------------------------------------------------- app integration
class FakeOutline:
    def __init__(self):
        self.keys = []
        self._id = 0

    async def create_key(self, name=None, limit_bytes=None):
        self._id += 1
        kid = str(self._id)
        self.keys.append({"id": kid, "name": name,
                          "dataLimit": {"bytes": limit_bytes} if limit_bytes else {}})
        return {"id": kid, "accessUrl": f"ss://abc@1.2.3.4:8388/#{name or ''}"}

    async def delete_key(self, kid):
        self.keys = [k for k in self.keys if k["id"] != kid]

    async def list_keys(self):
        return self.keys

    async def get_transfer_metrics(self):
        return {}

    async def get_server_metrics(self, since="30d"):
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("metrics off")

    async def close(self):
        pass


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "tma.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    from outline_panel.core.settings import BOT_ADMIN_IDS, BOT_TOKEN
    await deps.db.init()
    await deps.settings.set(BOT_TOKEN, TOKEN)
    await deps.settings.set(BOT_ADMIN_IDS, "777")
    deps.reg.servers["s1"] = {"id": "s1", "name": "Tokyo", "api_url": "https://x/y",
                              "cert_sha256": None, "api": FakeOutline()}
    yield appmod.app
    await deps.db.close()


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _hdr(init=None):
    return {"Authorization": "tma " + (init if init is not None else make_init_data())}


async def test_tma_requires_auth(app):
    c = await _client(app)
    assert (await c.get("/tma/api/keys")).status_code == 401
    await c.aclose()


async def test_tma_non_admin_forbidden(app):
    c = await _client(app)
    bad = make_init_data(user={"id": 555, "first_name": "Nope"})
    r = await c.get("/tma/api/keys", headers=_hdr(bad))
    assert r.status_code == 403
    await c.aclose()


async def test_tma_bootstrap_and_create(app):
    c = await _client(app)
    b = await c.get("/tma/api/bootstrap", headers=_hdr())
    assert b.status_code == 200
    assert b.json()["servers"][0]["name"] == "Tokyo"
    assert b.json()["user"]["id"] == 777

    r = await c.post("/tma/api/keys", headers=_hdr(),
                     json={"server": "s1", "name": "Reza", "limit_gb": 10, "days": 30})
    assert r.status_code == 200
    assert r.json()["accessUrl"].startswith("ss://")

    lst = await c.get("/tma/api/keys", headers=_hdr())
    keys = lst.json()["keys"]
    assert len(keys) == 1 and keys[0]["name"] == "Reza"
    await c.aclose()


async def test_tma_page_served(app):
    c = await _client(app)
    r = await c.get("/tma")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text
    await c.aclose()
