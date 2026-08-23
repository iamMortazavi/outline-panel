"""
Runtime settings stored in the DB (admin password hash, bot token, 2FA, …).

Bootstrapped from environment variables on first run, after which the panel is
fully self-configuring from its own UI.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

from . import config, security
from .db import DB

# setting keys
ADMIN_PW_HASH = "admin_password_hash"
ADMIN_PW_SALT = "admin_password_salt"
BOT_TOKEN = "bot_token"
BOT_ADMIN_IDS = "bot_admin_ids"   # comma-separated
BOT_ENABLED = "bot_enabled"       # "1"/"0"
TOTP_SECRET = "totp_secret"
TOTP_ENABLED = "totp_enabled"     # "1"/"0"
SUB_BASE_URL = "sub_base_url"
# Public base URL customers are given, e.g. https://star.example.com. Their
# profile is that host + "/<token>". Declared here and read through
# `get_profile_base()`; when it is set, that hostname serves the profile and
# nothing else — see web.profile.
PROFILE_BASE_URL = "profile_base_url"
WEBAPP_URL = "webapp_url"         # public https base, e.g. https://panel.example.com

# ---------------------------------------------------------------- panel knobs
# Every operational number the panel runs on. They were constants in five
# different modules and env-only variables, so tuning any of them meant editing
# code or a file on the server and restarting; the DB is the source of truth
# now, and env only seeds the first run.
#
# The spec travels with the value on purpose: /api/settings/panel serves it, so
# the UI renders whatever is in this table without a matching change in the
# frontend. Adding a knob is one row here plus one read at the point of use.
KNOBS: dict[str, dict] = {
    "notify_limit_percent": {
        "default": config.NOTIFY_LIMIT_PERCENT, "min": 0, "max": 100,
        "label": "Warn at % of data limit",
        "help": "Alert the key's admin once a user passes this share of their allowance.",
    },
    "notify_expiry_days": {
        "default": config.NOTIFY_EXPIRY_DAYS, "min": 0, "max": 365,
        "label": "Warn this many days before expiry",
        "help": "0 turns expiry warnings off.",
    },
    "expiry_check_interval": {
        "default": config.EXPIRY_CHECK_INTERVAL, "min": 10, "max": 86400,
        "label": "Scheduler interval (seconds)",
        "help": "How often to activate, reset, warn and expire. Lower means more "
                "load on every Outline server.",
    },
    "cycle_days": {
        "default": 30, "min": 1, "max": 365,
        "label": "Billing cycle (days)",
        "help": "Length of one monthly-quota period.",
    },
    "metrics_ttl": {
        "default": 15, "min": 0, "max": 3600,
        "label": "Metrics cache (seconds)",
        "help": "0 disables caching entirely — every dashboard poll then hits the "
                "experimental metrics endpoint of every server.",
    },
    "sub_update_hours": {
        "default": 12, "min": 1, "max": 168,
        "label": "Subscription refresh (hours)",
        "help": "How often VPN clients are told to re-fetch the subscription.",
    },
    "session_max_age": {
        "default": config.SESSION_MAX_AGE, "min": 300, "max": 31536000,
        "label": "Session lifetime (seconds)",
        "help": "How long a login stays valid.",
    },
    "login_max_fails": {
        "default": 5, "min": 1, "max": 1000,
        "label": "Failed logins per IP",
        "help": "Attempts allowed from one address inside the window below.",
    },
    "login_window": {
        "default": 300, "min": 10, "max": 86400,
        "label": "Login rate-limit window (seconds)",
    },
    "login_global_max_fails": {
        "default": 30, "min": 1, "max": 100000,
        "label": "Failed logins in total",
        "help": "Ceiling across every address, so rotating IPs can't walk past "
                "the per-IP limit.",
    },
    "health_alert_failures": {
        "default": 3, "min": 1, "max": 100,
        "label": "Alert after this many failed checks",
        "help": "Consecutive, not total — one missed check is a blip, and alerting "
                "on blips teaches people to ignore the alert.",
    },
    "health_retention_days": {
        "default": 14, "min": 1, "max": 365,
        "label": "Keep server health history for (days)",
    },
    "backup_interval_hours": {
        "default": 24, "min": 1, "max": 8760,
        "label": "Automatic backup every (hours)",
        "help": "Snapshots are taken with VACUUM INTO, which does not interrupt "
                "the panel. Turn the whole thing off with Backups enabled.",
    },
    "backup_keep": {
        "default": 14, "min": 1, "max": 1000,
        "label": "Backups to keep",
        "help": "Older snapshots are deleted once this many newer ones exist.",
    },
    "sub_cache_seconds": {
        "default": 20, "min": 0, "max": 3600,
        "label": "Subscription page cache (seconds)",
        "help": "The subscription link is public and each fetch reaches every "
                "server, so a client on a tight refresh loop multiplies load. "
                "0 disables caching.",
    },
    "sub_max_per_minute": {
        "default": 30, "min": 1, "max": 10000,
        "label": "Subscription fetches per minute, per address",
        "help": "Anything above this is refused with 429.",
    },
    "audit_retention_days": {
        "default": 90, "min": 1, "max": 3650,
        "label": "Keep the audit log for (days)",
        "help": "Older entries are deleted by the scheduler. The log only grows, "
                "so this is what stops it.",
    },
    "currency": {
        "default": "Toman", "type": "str", "max": 12,
        "label": "Currency label",
        "help": "Shown next to every price and balance.",
    },
}

# The owner's login name. Their password stays in ADMIN_PW_HASH/SALT above
# rather than in their admins row, so `outline-panel-admin reset-password`
# keeps working and there is one source of truth for it.
OWNER_USERNAME = "admin"


# Per-operation memo of settings already read. `None` means "no operation is in
# scope" — the scheduler, the bot and the CLI run that way and read straight
# through, exactly as before.
#
# A dict that lives for one HTTP request cannot go stale the way the old
# process-wide cache did: the bot polling with a token the panel had already
# replaced needed the cache to outlive the write, and this one is discarded
# before the response is sent. What it removes is the fan-out — `metrics_ttl`
# was read once per configured server inside `_conn_info`, and `profile_base`
# once per server inside `keys_for_server`, so a ten-server panel paid ~25
# redundant SELECTs on every poll of /api/keys.
_scope: ContextVar[dict[str, asyncio.Future[str | None]] | None] = ContextVar(
    "settings_scope", default=None)


@contextmanager
def read_scope():
    """Memoise settings reads for the duration of one operation.

    Lazy, not eager: a key is read from SQLite the first time it is asked for
    and remembered only until this block exits. Nesting is safe — an inner
    scope gets its own memo and the outer one is restored on the way out.
    """
    token = _scope.set({})
    try:
        yield
    finally:
        _scope.reset(token)


class SettingsStore:
    """Reads through to SQLite, memoised for the current operation only.

    There used to be a process-wide dict here. It was never invalidated, so the
    standalone bot kept polling with a token the panel had already replaced, and
    a restore had to reach in and clear it by hand. That cache is not coming
    back: `read_scope()` lives for one request and is thrown away, so no value
    can outlive the write that changed it.

    Outside a scope this behaves exactly as the uncached version did.
    """

    def __init__(self, db: DB):
        self.db = db

    async def get(self, key: str, default: str | None = None) -> str | None:
        memo = _scope.get()
        if memo is None:
            val = await self.db.get_setting(key)
            return val if val is not None else default
        # Futures, not values, so the memo is single-flight. `/api/keys` fans
        # out over the servers with `asyncio.gather`, and each branch reads
        # `metrics_ttl`; a plain dict would have every branch miss before any of
        # them had written, which is a cache that costs a lookup and saves
        # nothing. Claiming the slot happens before the first await, so exactly
        # one reader reaches SQLite.
        slot = memo.get(key)
        if slot is None:
            slot = asyncio.get_running_loop().create_future()
            memo[key] = slot
            try:
                slot.set_result(await self.db.get_setting(key))
            except BaseException as exc:
                slot.set_exception(exc)
                memo.pop(key, None)      # a failed read must not be remembered
                raise
        val = await slot
        return val if val is not None else default

    async def set(self, key: str, value: str | None) -> None:
        await self.db.set_setting(key, value)
        # A write inside a scope must be visible to the rest of it:
        # /api/settings/panel writes every knob and then re-reads them all to
        # build its response.
        memo = _scope.get()
        if memo is not None:
            done = asyncio.get_running_loop().create_future()
            done.set_result(value)
            memo[key] = done

    # panel knobs -----------------------------------------------------------
    async def num(self, key: str) -> int:
        """A numeric knob's current value, falling back to its env/spec default.

        A stored value outside the spec's range is ignored rather than obeyed:
        a hand-edited `expiry_check_interval` of 0 would spin the scheduler flat
        out against every Outline server.
        """
        spec = KNOBS[key]
        raw = await self.get(key)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return int(spec["default"])
        if val < spec["min"] or val > spec["max"]:
            return int(spec["default"])
        return val

    async def text(self, key: str) -> str:
        return (await self.get(key)) or str(KNOBS[key]["default"])

    async def knobs(self) -> dict[str, int | str]:
        return {
            k: (await self.text(k) if v.get("type") == "str" else await self.num(k))
            for k, v in KNOBS.items()
        }

    async def cycle_seconds(self) -> int:
        return await self.num("cycle_days") * 86400

    async def get_bool(self, key: str, default: bool = False) -> bool:
        v = await self.get(key)
        return default if v is None else v == "1"

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set(key, "1" if value else "0")

    async def get_admin_ids(self) -> set[int]:
        raw = await self.get(BOT_ADMIN_IDS, "") or ""
        return {int(x) for x in raw.split(",") if x.strip().isdecimal()}

    async def admin_for_telegram(self, uid: int | None) -> dict | None:
        """The panel admin behind a Telegram user, or None.

        A linked admin wins. Otherwise an id in the bot's own admin list is
        treated as the owner, which is exactly what it meant before admins
        existed — so nobody's bot access breaks the day this ships.
        """
        if uid is None:
            return None
        linked = await self.db.get_admin_by_telegram(int(uid))
        if linked:
            return None if linked["disabled"] else linked
        if int(uid) in await self.get_admin_ids():
            return await self.db.get_owner()
        return None

    async def get_profile_base(self) -> str | None:
        """Base URL of the customer profile site, or None when not configured."""
        url = (await self.get(PROFILE_BASE_URL) or "").strip().rstrip("/")
        return url or None

    async def get_profile_host(self) -> str | None:
        """Just the hostname, lowercased and without a port — what a Host header
        is compared against."""
        base = await self.get_profile_base()
        if not base:
            return None
        from urllib.parse import urlparse
        host = (urlparse(base).hostname or "").strip().lower()
        return host or None

    async def get_webapp_url(self) -> str | None:
        """Public HTTPS base URL of the panel, or None. The Mini App lives at
        ``<base>/tma``. Telegram only opens HTTPS Web App URLs."""
        url = (await self.get(WEBAPP_URL) or "").strip().rstrip("/")
        return url or None

    async def bootstrap(self) -> None:
        """Seed settings from env on first run; never overwrites existing values."""
        if await self.get(ADMIN_PW_HASH) is None and config.ADMIN_PASSWORD:
            h, s = await security.hash_password_async(config.ADMIN_PASSWORD)
            await self.set(ADMIN_PW_HASH, h)
            await self.set(ADMIN_PW_SALT, s)
        if await self.get(BOT_TOKEN) is None and config.BOT_TOKEN:
            await self.set(BOT_TOKEN, config.BOT_TOKEN)
            await self.set_bool(BOT_ENABLED, True)
        if await self.get(BOT_ADMIN_IDS) is None and config.ADMIN_IDS:
            await self.set(BOT_ADMIN_IDS, ",".join(str(i) for i in config.ADMIN_IDS))
        if await self.get(WEBAPP_URL) is None and config.WEBAPP_URL:
            await self.set(WEBAPP_URL, config.WEBAPP_URL.strip().rstrip("/"))
        # Give the existing password an identity. Upgrading an installed panel
        # lands here: same password, now reachable as username "admin".
        if await self.db.get_owner() is None:
            await self.db.add_admin(OWNER_USERNAME, None, None, is_owner=True)

    # password helpers ------------------------------------------------------
    async def verify_login(self, username: str, password: str) -> dict | None:
        """Return the admin row for a correct username+password, else None.

        Attempts are bounded by the login rate limiter (auth.py), which is what
        keeps the username-vs-password distinction from being a useful oracle.
        """
        row = await self.db.get_admin_by_username((username or "").strip())
        if not row or row["disabled"]:
            return None
        if row["is_owner"]:
            ok = await self.verify_admin_password(password)
        else:
            ok = await security.verify_password_async(
                password, row["pw_hash"] or "", row["pw_salt"] or "")
        return row if ok else None

    async def verify_admin_password(self, password: str) -> bool:
        h = await self.get(ADMIN_PW_HASH)
        s = await self.get(ADMIN_PW_SALT)
        if h and s:
            return await security.verify_password_async(password, h, s)
        # fallback: no DB password yet but env one is set
        import secrets as _secrets
        return bool(config.ADMIN_PASSWORD) and _secrets.compare_digest(
            password, config.ADMIN_PASSWORD
        )

    async def set_admin_password(self, password: str) -> None:
        h, s = await security.hash_password_async(password)
        await self.set(ADMIN_PW_HASH, h)
        await self.set(ADMIN_PW_SALT, s)
