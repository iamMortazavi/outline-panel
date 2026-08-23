"""The rules, with no I/O in them.

Nothing in this package may import `web`, `bot`, `aiosqlite`, `httpx` or
FastAPI. A state change is a method that returns commands; something else
carries them out. That is what makes the same rule reachable from the
dashboard, the Telegram bot, the Mini App and the scheduler without three
implementations of it.
"""
