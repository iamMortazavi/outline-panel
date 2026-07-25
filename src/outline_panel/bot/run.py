"""
Standalone Telegram bot entry point (`outline-panel-bot`).

Runs the *same* BotManager the panel runs in-process, on the same DB, registry
and settings singletons. It used to build its own dispatcher by hand and pass
neither `resolve_admin` nor `create_key`, which shipped two bugs:

  * every id in `bot_admin_ids` fell through to the test-only fallback in
    `dispatcher.admin_of` and was treated as the panel **owner** — full rights
    on every server, no scope, no credit;
  * `create_key` was None, so "New user" always answered "unavailable".

Sharing one wiring is what stops that recurring: there is no second copy to
forget to update.
"""

from __future__ import annotations

import asyncio
import logging

from ..core import config
from ..core.scheduler import expiry_loop
from ..core.settings import BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")


async def main() -> None:
    # Imported here, not at module scope: web.deps builds the DB/registry
    # singletons on import, and `--help` shouldn't pay for that.
    from ..web import deps

    await deps.db.init()
    await deps.settings.bootstrap()
    await deps.reg.load()

    token = await deps.settings.get(BOT_TOKEN) or config.BOT_TOKEN
    if not token:
        raise RuntimeError("No bot token configured (settings or BOT_TOKEN env).")

    username = await deps.botmgr.start(token)
    log.info("Telegram bot started as @%s", username)
    sched = asyncio.create_task(
        expiry_loop(deps.reg, deps.db, config.EXPIRY_CHECK_INTERVAL,
                    notifier=deps.botmgr.notify, settings=deps.settings)
    )
    try:
        await deps.botmgr.wait()   # BotManager owns polling; a crash surfaces here
    finally:
        sched.cancel()
        await deps.botmgr.stop()
        await deps.reg.close_all()
        await deps.db.close()


def cli() -> None:
    """Console-script entry point (`outline-panel-bot`)."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
