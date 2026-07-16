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
WEBAPP_URL = "webapp_url"         # public https base, e.g. https://panel.example.com

# The owner's login name. Their password stays in ADMIN_PW_HASH/SALT above
# rather than in their admins row, so `outline-panel-admin reset-password`
# keeps working and there is one source of truth for it.
OWNER_USERNAME = "admin"


class SettingsStore:
    def __init__(self, db: DB):
        self.db = db
        self._cache: dict[str, str | None] = {}

    async def get(self, key: str, default: str | None = None) -> str | None:
        if key not in self._cache:
            self._cache[key] = await self.db.get_setting(key)
        val = self._cache[key]
        return val if val is not None else default

    async def set(self, key: str, value: str | None) -> None:
        await self.db.set_setting(key, value)
        self._cache[key] = value

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
