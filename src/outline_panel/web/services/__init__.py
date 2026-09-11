"""Domain logic shared by the dashboard, the Mini App and the Telegram bot.

Sits between `state` (the shared objects) and `deps`/`routers` (the HTTP layer),
so the bot can create a key exactly the way the panel does without anything
having to import a router.
"""
