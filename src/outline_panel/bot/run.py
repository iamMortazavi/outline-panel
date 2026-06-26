"""
Standalone Telegram bot entry point (`outline-panel-bot`).

Shares the same multi-server Registry, DB and handlers as the in-process bot,
so running it separately is equivalent to enabling the bot from the panel.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo

from ..core import config
from ..core.db import DB
from ..core.scheduler import expiry_loop
from ..core.settings import BOT_TOKEN, SettingsStore
from ..web.registry import Registry
from .dispatcher import build_dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")


async def main() -> None:
    db = DB(config.DB_PATH)
    await db.init()
    settings = SettingsStore(db)
    await settings.bootstrap()
    reg = Registry(db)
    await reg.load()

    token = await settings.get(BOT_TOKEN) or config.BOT_TOKEN
    if not token:
        raise RuntimeError("No bot token configured (settings or BOT_TOKEN env).")

    async def get_admin_ids() -> set[int]:
        return await settings.get_admin_ids() or set(config.ADMIN_IDS)

    bot = Bot(token)

    async def notify(text: str) -> None:
        for aid in await get_admin_ids():
            try:
                await bot.send_message(aid, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("notify admin %s failed: %s", aid, e)

    dp = build_dispatcher(db, reg, get_admin_ids,
                          get_webapp_url=settings.get_webapp_url)
    wa_base = await settings.get_webapp_url()
    if wa_base and wa_base.startswith("https://"):
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Open", web_app=WebAppInfo(url=f"{wa_base}/tma")))
        except Exception as e:  # noqa: BLE001 — non-fatal
            log.warning("Could not set Web App menu button: %s", e)
    asyncio.create_task(
        expiry_loop(reg, db, config.EXPIRY_CHECK_INTERVAL, notifier=notify)
    )
    log.info("Telegram bot started.")
    try:
        await dp.start_polling(bot)
    finally:
        await reg.close_all()
        await bot.session.close()
        await db.close()


def cli() -> None:
    """Console-script entry point (`outline-panel-bot`)."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
