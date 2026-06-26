"""Read settings from environment variables (.env)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Environment variable '{name}' is not set. See .env.example.")
    return val


# Full Outline Management API URL, e.g.
# https://1.2.3.4:1234/AbCdEf12345
# Optional for the web app (servers can be added from the UI; this one is
# imported as the first server on first run). Required for the bot / CLI.
OUTLINE_API_URL: str | None = os.getenv("OUTLINE_API_URL")

# Optional: certSha256 from the Outline Manager config. If set, the server's
# TLS certificate is pinned to this fingerprint instead of skipping verification.
OUTLINE_CERT_SHA256: str | None = os.getenv("OUTLINE_CERT_SHA256")

# How often (seconds) to check for expired keys
EXPIRY_CHECK_INTERVAL: int = int(os.getenv("EXPIRY_CHECK_INTERVAL", "60"))

# Run the background expiry/notification scheduler in the web app. Set to
# "false" on the web app when the bot (which shares the DB) already runs it,
# so the two don't double-process the same keys.
ENABLE_SCHEDULER: bool = os.getenv("ENABLE_SCHEDULER", "true").lower() not in (
    "0", "false", "no", "off"
)

# Notify admins (bot only) when a key crosses this % of its data limit …
NOTIFY_LIMIT_PERCENT: int = int(os.getenv("NOTIFY_LIMIT_PERCENT", "80"))
# … or when it has this many days (or fewer) left before expiry.
NOTIFY_EXPIRY_DAYS: int = int(os.getenv("NOTIFY_EXPIRY_DAYS", "3"))

# SQLite database path
DB_PATH: str = os.getenv("DB_PATH", "outline_bot.db")

# --- Telegram bot only (optional if you only run the web app) -------------
BOT_TOKEN: str | None = os.getenv("BOT_TOKEN")
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# --- Web app only (optional if you only run the bot) ----------------------
# Password required to log into the web dashboard.
ADMIN_PASSWORD: str | None = os.getenv("ADMIN_PASSWORD")
# Secret used to sign session cookies. Auto-generated if not set (sessions
# reset on restart). Set a fixed value in production — and REQUIRED when running
# with more than one worker, otherwise each worker signs with a different key.
SESSION_SECRET_SET: bool = bool(os.getenv("SESSION_SECRET"))
SESSION_SECRET: str = os.getenv("SESSION_SECRET") or os.urandom(32).hex()
# How long a login session stays valid (seconds). Default: 7 days.
SESSION_MAX_AGE: int = int(os.getenv("SESSION_MAX_AGE", str(7 * 86400)))
# Send the session cookie only over HTTPS. Keep "true" in production; set to
# "false" for local development over plain HTTP.
COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "true").lower() not in (
    "0", "false", "no", "off"
)


def require_api_url() -> str:
    if not OUTLINE_API_URL:
        raise RuntimeError("OUTLINE_API_URL is not set. See .env.example.")
    return OUTLINE_API_URL


def require_bot_token() -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. See .env.example.")
    return BOT_TOKEN


def require_admin_password() -> str:
    if not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN_PASSWORD is not set. See .env.example.")
    return ADMIN_PASSWORD
