import os
import sys
import tempfile

import httpx
import pytest

from aiogram import Dispatcher

from outline_panel.bot.core import build_dispatcher


class _DummyReg:
    def ids(self):
        return []

    def get(self, sid):
        return None

    def meta(self, sid):
        return None


def test_build_dispatcher_registers_handlers():
    dp = build_dispatcher(db=None, registry=_DummyReg(), get_admin_ids=lambda: {1})
    assert isinstance(dp, Dispatcher)
    assert len(dp.message.handlers) > 0
    assert len(dp.callback_query.handlers) > 0


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


async def test_bot_settings_save_starts_bot(app, monkeypatch):
    application, deps = app
    started = {}

    async def fake_start(token):
        started["token"] = token
        return "MyBot"

    async def fake_stop():
        started["stopped"] = True

    monkeypatch.setattr(deps.botmgr, "start", fake_start)
    monkeypatch.setattr(deps.botmgr, "stop", fake_stop)
    monkeypatch.setattr(type(deps.botmgr), "running", property(lambda self: True))

    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await c.post("/api/login", json={"password": "pw"})
        r = await c.put("/api/settings/bot", json={
            "token": "123456:ABCDEF_token_value_long", "adminIds": "111, 222, bad",
            "enabled": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True and body["enabled"] is True
        assert body["adminIds"] == [111, 222]          # non-numeric dropped
        assert started["token"] == "123456:ABCDEF_token_value_long"

        # disabling stops the bot
        r = await c.put("/api/settings/bot", json={"adminIds": "111,222", "enabled": False})
        assert r.status_code == 200
        assert started.get("stopped") is True


async def test_bot_settings_disabled_by_default(app):
    application, deps = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        await c.post("/api/login", json={"password": "pw"})
        body = (await c.get("/api/settings/bot")).json()
        assert body["configured"] is False
        assert body["enabled"] is False
        assert body["running"] is False
