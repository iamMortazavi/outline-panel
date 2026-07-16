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

    async def rename_key(self, kid, name):
        for k in self.keys:
            if k["id"] == kid:
                k["name"] = name

    async def set_data_limit(self, kid, limit_bytes):
        for k in self.keys:
            if k["id"] == kid:
                k["dataLimit"] = {"bytes": limit_bytes}

    async def remove_data_limit(self, kid):
        for k in self.keys:
            if k["id"] == kid:
                k["dataLimit"] = {}

    async def list_keys(self):
        return self.keys

    async def get_transfer_metrics(self):
        return {}

    async def get_server_metrics(self, since="30d"):
        from outline_panel.core.outline_api import OutlineError
        raise OutlineError("metrics off")

    async def get_server_metrics_cached(self, since="30d", ttl=15.0):
        return await self.get_server_metrics(since)

    async def close(self):
        pass


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "tma.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.core.settings import BOT_ADMIN_IDS, BOT_TOKEN
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()  # creates the owner login
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


async def test_tma_stats(app):
    c = await _client(app)
    r = await c.get("/tma/api/stats", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert "perServer" in body and body["serverCount"] == 1
    await c.aclose()


async def test_tma_edit_rename_and_delete(app):
    c = await _client(app)
    await c.post("/tma/api/keys", headers=_hdr(),
                 json={"server": "s1", "name": "Sara", "limit_gb": 5, "days": 0})
    # rename
    r = await c.put("/tma/api/keys/s1/1/name", headers=_hdr(), json={"name": "Sara2"})
    assert r.status_code == 200
    keys = (await c.get("/tma/api/keys", headers=_hdr())).json()["keys"]
    assert keys[0]["name"] == "Sara2"
    # disable / enable
    assert (await c.post("/tma/api/keys/s1/1/disable", headers=_hdr())).status_code == 200
    assert (await c.post("/tma/api/keys/s1/1/enable", headers=_hdr())).status_code == 200
    # delete
    assert (await c.delete("/tma/api/keys/s1/1", headers=_hdr())).status_code == 200
    assert (await c.get("/tma/api/keys", headers=_hdr())).json()["keys"] == []
    await c.aclose()


async def test_tma_edit_requires_admin(app):
    c = await _client(app)
    bad = make_init_data(user={"id": 555, "first_name": "Nope"})
    r = await c.put("/tma/api/keys/s1/1/name", headers=_hdr(bad), json={"name": "x"})
    assert r.status_code == 403
    await c.aclose()


# ------------------------------------------- Telegram now carries panel rights
async def _link_sub(deps, tg_id, caps="keys.view", servers="s1", credit=0, discount=0):
    from outline_panel.core import security
    h, s = security.hash_password("x")
    aid = await deps.db.add_admin("sara", h, s, caps=caps, servers=servers)
    await deps.db.update_admin(aid, telegram_id=tg_id,
                               credit_enabled=1 if credit else 0,
                               discount_pct=discount)
    if credit:
        await deps.db.credit_admin(aid, credit, reason="topup")
    return aid


def _as(tg_id, name="Sara"):
    return {"Authorization": "tma " + make_init_data(user={"id": tg_id, "first_name": name})}


async def test_unlinked_bot_admin_still_has_full_access(app):
    """777 is in bot_admin_ids but linked to nobody — it must keep working
    exactly as it did before admins existed, or every bot user breaks on deploy."""
    c = await _client(app)
    me = (await c.get("/tma/api/bootstrap", headers=_hdr())).json()["me"]
    assert me["isOwner"] is True and "keys.create" in me["caps"]
    await c.aclose()


async def test_a_linked_sub_admin_gets_only_their_caps(app):
    from outline_panel.web import deps
    await _link_sub(deps, 888, caps="keys.view")
    c = await _client(app)
    me = (await c.get("/tma/api/bootstrap", headers=_as(888))).json()["me"]
    assert me["isOwner"] is False and me["caps"] == ["keys.view"]

    assert (await c.get("/tma/api/keys", headers=_as(888))).status_code == 200
    assert (await c.post("/tma/api/keys", headers=_as(888),
                         json={"server": "s1", "name": "X"})).status_code == 403
    assert (await c.delete("/tma/api/keys/s1/1", headers=_as(888))).status_code == 403
    await c.aclose()


async def test_telegram_respects_server_scope(app):
    from outline_panel.web import deps
    deps.reg.servers["s2"] = {"id": "s2", "name": "Berlin", "api_url": "https://x/y",
                              "cert_sha256": None, "api": FakeOutline()}
    await _link_sub(deps, 888, caps="keys.view,keys.create", servers="s1")
    c = await _client(app)
    boot = (await c.get("/tma/api/bootstrap", headers=_as(888))).json()
    assert [s["id"] for s in boot["servers"]] == ["s1"]      # s2 does not exist to her
    assert (await c.post("/tma/api/keys", headers=_as(888),
                         json={"server": "s2", "name": "X"})).status_code == 404
    await c.aclose()


async def test_a_disabled_admin_loses_telegram_too(app):
    from outline_panel.web import deps
    aid = await _link_sub(deps, 888)
    c = await _client(app)
    assert (await c.get("/tma/api/keys", headers=_as(888))).status_code == 200
    await deps.db.update_admin(aid, disabled=1)
    assert (await c.get("/tma/api/keys", headers=_as(888))).status_code == 403
    await c.aclose()


async def test_telegram_cannot_create_around_the_price_list(app):
    """The whole point: a free-form create over Telegram would hand a reseller
    unlimited free keys and never touch their credit."""
    from outline_panel.web import deps
    aid = await _link_sub(deps, 888, caps="keys.view,keys.create", credit=100_000)
    pid = await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _client(app)

    # no package named → refused, not silently created free-form
    r = await c.post("/tma/api/keys", headers=_as(888),
                     json={"server": "s1", "name": "X", "limit_gb": 500, "days": 3650})
    assert r.status_code == 400 and "package" in r.json()["detail"].lower()
    assert (await deps.db.get_admin(aid))["credit"] == 100_000

    # with a package: charged, and the package decides the size
    r = await c.post("/tma/api/keys", headers=_as(888),
                     json={"server": "s1", "name": "X", "package_id": pid,
                           "limit_gb": 500, "days": 3650})
    assert r.status_code == 200
    assert r.json()["limit"] == 5 * 1024 ** 3
    assert (await deps.db.get_admin(aid))["credit"] == 70_000
    await c.aclose()


async def test_telegram_shows_the_discounted_price_list(app):
    from outline_panel.web import deps
    await _link_sub(deps, 888, caps="keys.view,keys.create", credit=100_000, discount=10)
    await deps.db.add_package("5 GB", 5, 30, 30_000)
    c = await _client(app)
    d = (await c.get("/tma/api/packages", headers=_as(888))).json()
    assert d["creditEnabled"] is True and d["credit"] == 100_000
    assert d["packages"][0]["price"] == 27_000 and d["packages"][0]["affordable"] is True
    await c.aclose()


async def test_telegram_sees_only_their_own_users(app):
    from outline_panel.web import deps
    aid = await _link_sub(deps, 888, caps="keys.view,keys.create")
    c = await _client(app)
    # the list is built from what Outline returns, matched against our rows —
    # so the fake needs the keys too, not just the DB
    fake = deps.reg.servers["s1"]["api"]
    fake.keys = [{"id": "1", "name": "Hers", "accessUrl": "ss://a", "dataLimit": {}},
                 {"id": "2", "name": "Owner's", "accessUrl": "ss://b", "dataLimit": {}}]
    await deps.db.add_key("s1", "1", "Hers", None, None, owner_admin_id=aid)
    await deps.db.add_key("s1", "2", "Owner's", None, None)
    names = [k["name"] for k in (await c.get("/tma/api/keys", headers=_as(888))).json()["keys"]]
    assert names == ["Hers"]
    # the unlinked bot admin (the owner) sees both
    allk = [k["name"] for k in (await c.get("/tma/api/keys", headers=_hdr())).json()["keys"]]
    assert sorted(allk) == ["Hers", "Owner's"]
    await c.aclose()
