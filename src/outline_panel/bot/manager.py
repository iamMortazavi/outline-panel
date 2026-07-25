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
    def __init__(self, db, registry, get_admin_ids, get_webapp_url=None,
                 resolve_admin=None, create_key=None):
        self.db = db
        self.registry = registry
        self.get_admin_ids = get_admin_ids
        self.get_webapp_url = get_webapp_url
        # Injected so the bot decides access and creates keys with the same code
        # the panel does; importing web.deps from here would be a cycle.
        self.resolve_admin = resolve_admin
        self.create_key = create_key
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
                              self.notify, self.get_webapp_url,
                              self.resolve_admin, self.create_key)
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

    async def wait(self) -> None:
        """Block until polling stops. Used by the standalone entry point, which
        has nothing else to keep the loop alive."""
        if self._task is not None:
            await self._task

    async def _global_ids(self) -> set[int]:
        ids = self.get_admin_ids()
        if hasattr(ids, "__await__"):
            ids = await ids
        return set(ids or ())

    async def _targets(self, owner_admin_id: int | None) -> set[int]:
        """Who hears about one key's alert.

        A reseller's customer is the reseller's problem, not the whole admin
        list's — and previously the owner got every sub-admin's alerts while the
        sub-admin got none. An owner-less key (the panel owner's) still goes to
        the configured bot admins, and so does an unlinked admin's, so an alert
        is never dropped for want of a Telegram id.
        """
        if owner_admin_id is None:
            return await self._global_ids()
        row = await self.db.get_admin(owner_admin_id)
        if row and row["telegram_id"]:
            return {int(row["telegram_id"])}
        return await self._global_ids()

    async def notify(self, text: str, owner_admin_id: int | None = None) -> None:
        """Scheduler notifier — message whoever owns the key (best effort)."""
        if self._bot is None:
            return
        for aid in await self._targets(owner_admin_id):
            try:
                await self._bot.send_message(aid, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("notify admin %s failed: %s", aid, e)
