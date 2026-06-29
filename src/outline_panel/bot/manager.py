"""
Start/stop the Telegram bot inside the web process, driven by panel settings.

The token and admin IDs live in the DB (settings), so the operator configures
the bot entirely from the dashboard — no env editing or service restart.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo

from .dispatcher import build_dispatcher

log = logging.getLogger("bot.manager")


class BotManager:
    def __init__(self, db, registry, get_admin_ids, get_webapp_url=None):
        self.db = db
        self.registry = registry
        self.get_admin_ids = get_admin_ids
        self.get_webapp_url = get_webapp_url
        self._bot: Bot | None = None
        self._dp = None
        self._task: asyncio.Task | None = None
        self._username: str | None = None
        # serialize start/stop so concurrent calls can't leak a Bot session or
        # spawn a duplicate polling task
        self._lifecycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {"running": self.running, "username": self._username}

    async def validate_token(self, token: str) -> str:
        """Return the bot @username, or raise on an invalid token."""
        probe = Bot(token)
        try:
            me = await probe.get_me()
            return me.username
        finally:
            await probe.session.close()

    async def _resolve_webapp_url(self) -> str | None:
        if self.get_webapp_url is None:
            return None
        res = self.get_webapp_url()
        if hasattr(res, "__await__"):
            res = await res
        return res if (res and res.startswith("https://")) else None

    async def start(self, token: str) -> str:
        async with self._lifecycle_lock:
            return await self._start_locked(token)

    async def _start_locked(self, token: str) -> str:
        await self._stop_locked()
        bot = Bot(token)
        me = await bot.get_me()  # validates the token
        dp = build_dispatcher(self.db, self.registry, self.get_admin_ids,
                              self.notify, self.get_webapp_url)
        self._bot, self._dp, self._username = bot, dp, me.username
        # Persistent chat menu button → opens the Mini App (best effort).
        wa_url = await self._resolve_webapp_url()
        if wa_url:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Open", web_app=WebAppInfo(url=f"{wa_url}/tma")))
            except Exception as e:  # noqa: BLE001 — non-fatal
                log.warning("Could not set Web App menu button: %s", e)
        self._task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False)
        )
        log.info("Telegram bot started as @%s", me.username)
        return me.username

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._dp is not None:
            try:
                await self._dp.stop_polling()
            except Exception:  # noqa: BLE001 — may not be polling yet
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._bot is not None:
            await self._bot.session.close()
        self._bot = self._dp = self._task = self._username = None

    async def notify(self, text: str) -> None:
        """Scheduler notifier — message every admin (best effort)."""
        if self._bot is None:
            return
        ids = self.get_admin_ids()
        if hasattr(ids, "__await__"):
            ids = await ids
        for aid in ids or ():
            try:
                await self._bot.send_message(aid, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("notify admin %s failed: %s", aid, e)
