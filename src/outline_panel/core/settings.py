"""
Runtime settings stored in the DB (admin password hash, bot token, 2FA, …).

Bootstrapped from environment variables on first run, after which the panel is
fully self-configuring from its own UI.
"""

from __future__ import annotations

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


# How a stored string becomes a knob's value. Shared by the store and the view
# below so the two can never disagree about what a setting means — the range
# check in particular, which is the difference between an interval of 60 and a
# scheduler spinning flat out against every Outline server.
def _knob_num(spec: dict, raw: str | None) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return int(spec["default"])
    if val < spec["min"] or val > spec["max"]:
        return int(spec["default"])
    return val


def _knob_text(spec: dict, raw: str | None) -> str:
    return raw or str(spec["default"])


def _norm_base(raw: str | None) -> str | None:
    url = (raw or "").strip().rstrip("/")
    return url or None


class SettingsView:
    """The settings table as it was at one moment, read once.

    `SettingsStore` goes to SQLite on every single call, deliberately: a cache
    that lived as long as the process is what left the standalone bot polling
    with a token the panel had already replaced. That default is right and it
    stays.

    What it is not built for is being asked the same question repeatedly inside
    one unit of work. `knobs()` asked twenty times to serve one request, and
    `/api/keys` re-read the metrics TTL and the profile base once per server, so
    the cost of listing keys grew with the size of the fleet for no reason.

    A view settles both: one SELECT, and a lifetime — a single request, a single
    scheduler pass — short enough that there is no staleness to reason about.
    Build one with ``await store.view()``; it is a plain dict underneath, so
    every accessor here is sync.
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, str]):
        self._values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        val = self._values.get(key)
        return val if val is not None else default

    def num(self, key: str) -> int:
        return _knob_num(KNOBS[key], self.get(key))

    def text(self, key: str) -> str:
        return _knob_text(KNOBS[key], self.get(key))

    def knobs(self) -> dict[str, int | str]:
        return {
            k: (self.text(k) if v.get("type") == "str" else self.num(k))
            for k, v in KNOBS.items()
        }

    def cycle_seconds(self) -> int:
        return self.num("cycle_days") * 86400

    def profile_base(self) -> str | None:
        return _norm_base(self.get(PROFILE_BASE_URL))


class SettingsStore:
    """Reads straight through to SQLite — deliberately uncached.

    There used to be an in-process dict here. It was never invalidated, so the
    standalone bot kept polling with a token the panel had already replaced, and
    a restore had to reach in and clear it by hand. A local SQLite row read costs
    microseconds; a settings value that disagrees with the database costs an
    afternoon.
    """

    def __init__(self, db: DB):
        self.db = db

    async def get(self, key: str, default: str | None = None) -> str | None:
        val = await self.db.get_setting(key)
        return val if val is not None else default

    async def set(self, key: str, value: str | None) -> None:
        await self.db.set_setting(key, value)

    async def view(self) -> SettingsView:
        """Every setting in one read, for a caller that needs several.

        Use it wherever more than one setting is consulted to do one thing —
        a request, a scheduler pass. See `SettingsView` for why this is a view
        and not a cache.
        """
        return SettingsView(await self.db.all_settings())

    # panel knobs -----------------------------------------------------------
    async def num(self, key: str) -> int:
        """A numeric knob's current value, falling back to its env/spec default.

        A stored value outside the spec's range is ignored rather than obeyed:
        a hand-edited `expiry_check_interval` of 0 would spin the scheduler flat
        out against every Outline server.
        """
        return _knob_num(KNOBS[key], await self.get(key))

    async def text(self, key: str) -> str:
        return _knob_text(KNOBS[key], await self.get(key))

    async def knobs(self) -> dict[str, int | str]:
        # One SELECT for the lot; this used to be two row reads per knob.
        return (await self.view()).knobs()

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
        return _norm_base(await self.get(PROFILE_BASE_URL))

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
            h, s = security.hash_password(config.ADMIN_PASSWORD)
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
            ok = security.verify_password(password, row["pw_hash"] or "",
                                          row["pw_salt"] or "")
        return row if ok else None

    async def verify_admin_password(self, password: str) -> bool:
        h = await self.get(ADMIN_PW_HASH)
        s = await self.get(ADMIN_PW_SALT)
        if h and s:
            return security.verify_password(password, h, s)
        # fallback: no DB password yet but env one is set
        import secrets as _secrets
        return bool(config.ADMIN_PASSWORD) and _secrets.compare_digest(
            password, config.ADMIN_PASSWORD
        )

    async def set_admin_password(self, password: str) -> None:
        h, s = security.hash_password(password)
        await self.set(ADMIN_PW_HASH, h)
        await self.set(ADMIN_PW_SALT, s)
