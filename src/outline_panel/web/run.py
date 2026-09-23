"""Console-script entry point for the web dashboard (`outline-panel`)."""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def cli() -> None:
    import uvicorn

    # Always one worker: the scheduler and the Telegram bot run inside this
    # process, and the database is SQLite — a second worker would run both
    # twice. One asyncio process on uvloop/httptools is plenty for a panel.
    uvicorn.run(
        "outline_panel.web.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        # the panel keeps its own audit log; a line per request is CPU and
        # disk a small VPS does not have to spare
        access_log=_flag("ACCESS_LOG", False),
        server_header=False,
        # trust X-Forwarded-* only from the local reverse proxy
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_keep_alive=int(os.getenv("KEEP_ALIVE", "15")),
        # a ceiling, so a burst queues at the proxy rather than exhausting RAM
        limit_concurrency=int(os.getenv("LIMIT_CONCURRENCY", "200")),
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    cli()
