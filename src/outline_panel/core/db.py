"""
SQLite storage with multi-server support.

Two tables:
  • servers — the configured Outline servers (local name + apiUrl)
  • keys    — per-key metadata, keyed by the composite (server_id, key_id)

Time model: "validity starts on first connection". On creation only
duration_days is stored; as soon as traffic is seen the scheduler activates the
key and sets expiry_ts.

A single persistent connection (WAL mode) is used so the dashboard and the
scheduler don't hit "database is locked"; writes are serialized with a lock.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite

_SERVERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    api_url     TEXT,
    cert_sha256 TEXT,
    created_ts  INTEGER
);
"""

_KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    server_id     TEXT,
    key_id        TEXT,
    name          TEXT,
    limit_bytes   INTEGER,
    duration_days INTEGER,
    activated_ts  INTEGER,
    expiry_ts     INTEGER,
    disabled      INTEGER DEFAULT 0,
    monthly_bytes INTEGER,
    reset_ts      INTEGER,
    sub_token     TEXT,
    created_ts    INTEGER,
    owner_admin_id INTEGER,
    PRIMARY KEY (server_id, key_id)
);
"""

_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Panel logins. The owner (is_owner=1) always exists and always has every right;
# `servers`/`caps` are csv and only constrain sub-admins. The owner's password
# lives in `settings` (admin_password_hash/salt) so the reset-password CLI keeps
# working — pw_hash/pw_salt here are for sub-admins only.
_ADMINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT,
    pw_salt    TEXT,
    is_owner   INTEGER DEFAULT 0,
    caps       TEXT DEFAULT '',
    servers    TEXT DEFAULT '',
    disabled   INTEGER DEFAULT 0,
    created_ts INTEGER
);
"""


# What a sub-admin may sell. `price` is the base in Toman, before the admin's
# personal discount.
_PACKAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    gb         REAL,
    days       INTEGER,
    monthly_gb REAL,
    price      INTEGER NOT NULL,
    created_ts INTEGER
);
"""

# Every movement of credit, ever. `admins.credit` is the atomic guard the
# purchase checks against; this is the answer to "where did my money go".
# package_name/price are SNAPSHOTS: history must survive renaming or deleting
# a package, so nothing here joins back to `packages`.
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id      INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    package_id    INTEGER,
    package_name  TEXT,
    price_before_discount INTEGER,
    server_id     TEXT,
    key_id        TEXT,
    note          TEXT,
    created_ts    INTEGER
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("DB.init() must be awaited before use.")
        return self._db

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL allows concurrent reads/writes without locking the whole DB
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(_SERVERS_SCHEMA)
        # migration: add the cert_sha256 column to older servers tables
        cur = await self._db.execute("PRAGMA table_info(servers)")
        scols = [r[1] for r in await cur.fetchall()]
        if "cert_sha256" not in scols:
            await self._db.execute("ALTER TABLE servers ADD COLUMN cert_sha256 TEXT")
        # migrate a legacy single-server keys table (no server_id), if present
        cur = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='keys'"
        )
        exists = await cur.fetchone()
        if exists:
            cur = await self._db.execute("PRAGMA table_info(keys)")
            cols = [r[1] for r in await cur.fetchall()]
            if "server_id" not in cols:
                await self._migrate_keys(cols)
        else:
            await self._db.execute(_KEYS_SCHEMA)
        # migration: add the monthly-quota columns to older keys tables
        cur = await self._db.execute("PRAGMA table_info(keys)")
        kcols = [r[1] for r in await cur.fetchall()]
        for col in ("monthly_bytes", "reset_ts"):
            if col not in kcols:
                await self._db.execute(f"ALTER TABLE keys ADD COLUMN {col} INTEGER")
        if "sub_token" not in kcols:
            await self._db.execute("ALTER TABLE keys ADD COLUMN sub_token TEXT")
        # migration: which admin a key belongs to. NULL = the panel owner's,
        # so every key that already exists stays theirs with no backfill.
        if "owner_admin_id" not in kcols:
            await self._db.execute(
                "ALTER TABLE keys ADD COLUMN owner_admin_id INTEGER"
            )
        # runtime settings table (password hash, bot token, 2FA, ...)
        await self._db.execute(_SETTINGS_SCHEMA)
        # panel logins (owner + sub-admins)
        await self._db.execute(_ADMINS_SCHEMA)
        # migration: credit columns on older admins tables. credit_enabled
        # defaults to 0, so every admin that already exists keeps its current
        # behaviour — opting in is a deliberate act.
        cur = await self._db.execute("PRAGMA table_info(admins)")
        acols = [r[1] for r in await cur.fetchall()]
        for col in ("credit", "credit_enabled", "discount_pct"):
            if col not in acols:
                await self._db.execute(
                    f"ALTER TABLE admins ADD COLUMN {col} INTEGER DEFAULT 0"
                )
        # migration: which Telegram user this admin is, so the bot and Mini App
        # can apply the same rights the dashboard does. NULL = not linked.
        if "telegram_id" not in acols:
            await self._db.execute("ALTER TABLE admins ADD COLUMN telegram_id INTEGER")
        # resale packages + the credit ledger
        await self._db.execute(_PACKAGES_SCHEMA)
        await self._db.execute(_LEDGER_SCHEMA)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_admin ON credit_ledger(admin_id)"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _migrate_keys(self, old_cols: list[str]) -> None:
        # legacy keys are attributed to the 'default' server
        await self.conn.execute("ALTER TABLE keys RENAME TO keys_old")
        await self.conn.execute(_KEYS_SCHEMA)
        carry = [c for c in (
            "key_id", "name", "limit_bytes", "duration_days",
            "activated_ts", "expiry_ts", "disabled", "created_ts",
        ) if c in old_cols]
        sel = ", ".join(carry)
        await self.conn.execute(
            f"INSERT INTO keys (server_id, {sel}) "
            f"SELECT 'default', {sel} FROM keys_old"
        )
        await self.conn.execute("DROP TABLE keys_old")

    # servers ---------------------------------------------------------------
    async def add_server(self, sid: str, name: str, api_url: str,
                         cert_sha256: str | None = None) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO servers "
                "(id, name, api_url, cert_sha256, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, name, api_url, cert_sha256, int(time.time())),
            )
            await self.conn.commit()

    async def all_servers(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM servers ORDER BY created_ts")
        return [dict(r) for r in await cur.fetchall()]

    async def get_server(self, sid: str) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM servers WHERE id = ?", (sid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def rename_server_local(self, sid: str, name: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE servers SET name = ? WHERE id = ?", (name, sid)
            )
            await self.conn.commit()

    async def delete_server(self, sid: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM servers WHERE id = ?", (sid,))
            await self.conn.execute("DELETE FROM keys WHERE server_id = ?", (sid,))
            await self.conn.commit()

    # keys ------------------------------------------------------------------
    async def add_key(
        self, server_id: str, key_id: str, name: str,
        limit_bytes: int | None, duration_days: int | None,
        owner_admin_id: int | None = None,
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO keys "
                "(server_id, key_id, name, limit_bytes, duration_days, "
                " activated_ts, expiry_ts, disabled, created_ts, owner_admin_id) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)",
                (server_id, key_id, name, limit_bytes, duration_days,
                 int(time.time()), owner_admin_id),
            )
            await self.conn.commit()

    async def set_key_owner(self, server_id: str, key_id: str,
                            owner_admin_id: int | None) -> None:
        await self._update(server_id, key_id, "owner_admin_id", owner_admin_id)

    async def get_key(self, server_id: str, key_id: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM keys WHERE server_id = ? AND key_id = ?",
            (server_id, key_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def keys_for(self, server_id: str) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM keys WHERE server_id = ?", (server_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def all_keys(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM keys")
        return [dict(r) for r in await cur.fetchall()]

    async def delete_key(self, server_id: str, key_id: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM keys WHERE server_id = ? AND key_id = ?",
                (server_id, key_id),
            )
            await self.conn.commit()

    async def _update(self, server_id: str, key_id: str, field: str, value) -> None:
        async with self._lock:
            await self.conn.execute(
                f"UPDATE keys SET {field} = ? WHERE server_id = ? AND key_id = ?",
                (value, server_id, key_id),
            )
            await self.conn.commit()

    async def set_disabled(self, server_id, key_id, disabled: bool):
        await self._update(server_id, key_id, "disabled", 1 if disabled else 0)

    async def set_name(self, server_id, key_id, name: str):
        await self._update(server_id, key_id, "name", name)

    async def set_limit(self, server_id, key_id, limit_bytes: int | None):
        await self._update(server_id, key_id, "limit_bytes", limit_bytes)

    async def set_duration(self, server_id, key_id, duration_days: int | None):
        await self._update(server_id, key_id, "duration_days", duration_days)

    async def set_expiry(self, server_id, key_id, expiry_ts: int | None):
        await self._update(server_id, key_id, "expiry_ts", expiry_ts)

    async def set_reset(self, server_id, key_id, reset_ts: int | None):
        await self._update(server_id, key_id, "reset_ts", reset_ts)

    async def set_sub_token(self, server_id, key_id, token: str | None):
        await self._update(server_id, key_id, "sub_token", token)

    async def get_key_by_sub_token(self, token: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM keys WHERE sub_token = ?", (token,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_keys_by_sub_token(self, token: str) -> list[dict]:
        """All keys sharing a subscription token (a multi-server subscription)."""
        cur = await self.conn.execute(
            "SELECT * FROM keys WHERE sub_token = ? ORDER BY created_ts", (token,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_monthly(self, server_id, key_id,
                          monthly_bytes: int | None, reset_ts: int | None):
        async with self._lock:
            await self.conn.execute(
                "UPDATE keys SET monthly_bytes = ?, reset_ts = ? "
                "WHERE server_id = ? AND key_id = ?",
                (monthly_bytes, reset_ts, server_id, key_id),
            )
            await self.conn.commit()

    async def activate(self, server_id, key_id, activated_ts: int, expiry_ts: int):
        async with self._lock:
            await self.conn.execute(
                "UPDATE keys SET activated_ts = ?, expiry_ts = ? "
                "WHERE server_id = ? AND key_id = ?",
                (activated_ts, expiry_ts, server_id, key_id),
            )
            await self.conn.commit()

    async def pending_activation_keys(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM keys "
            "WHERE duration_days IS NOT NULL AND activated_ts IS NULL"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def expired_active_keys(self, now_ts: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM keys "
            "WHERE expiry_ts IS NOT NULL AND expiry_ts <= ? AND disabled = 0",
            (now_ts,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # key/value settings ----------------------------------------------------
    async def get_setting(self, key: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await self.conn.commit()

    async def all_settings(self) -> dict[str, str]:
        cur = await self.conn.execute("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in await cur.fetchall()}

    # panel logins ----------------------------------------------------------
    async def add_admin(self, username: str, pw_hash: str, pw_salt: str,
                        caps: str = "", servers: str = "",
                        is_owner: bool = False) -> int:
        async with self._lock:
            cur = await self.conn.execute(
                "INSERT INTO admins (username, pw_hash, pw_salt, is_owner, caps,"
                " servers, disabled, created_ts) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (username, pw_hash, pw_salt, 1 if is_owner else 0, caps, servers,
                 int(time.time())),
            )
            await self.conn.commit()
            return cur.lastrowid

    async def get_admin(self, admin_id: int) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_admin_by_username(self, username: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM admins WHERE username = ?", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_admin_by_telegram(self, telegram_id: int) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM admins WHERE telegram_id = ?", (int(telegram_id),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_owner(self) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM admins WHERE is_owner = 1 ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def all_admins(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM admins ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def update_admin(self, admin_id: int, **fields) -> None:
        """Update only the named columns. Unknown ones are ignored, so a caller
        can pass a whole request body without smuggling in `is_owner`."""
        allowed = ("username", "pw_hash", "pw_salt", "caps", "servers", "disabled",
                   "credit_enabled", "discount_pct", "telegram_id")
                   # never "credit": it moves only through the ledger
        cols = [c for c in allowed if c in fields]
        if not cols:
            return
        async with self._lock:
            await self.conn.execute(
                f"UPDATE admins SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [admin_id],
            )
            await self.conn.commit()

    async def delete_admin(self, admin_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM admins WHERE id = ? AND is_owner = 0", (admin_id,)
            )
            await self.conn.commit()

    # packages --------------------------------------------------------------
    async def add_package(self, name: str, gb: float | None, days: int | None,
                          price: int, monthly_gb: float | None = None) -> int:
        async with self._lock:
            cur = await self.conn.execute(
                "INSERT INTO packages (name, gb, days, monthly_gb, price, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, gb, days, monthly_gb, int(price), int(time.time())),
            )
            await self.conn.commit()
            return cur.lastrowid

    async def get_package(self, pkg_id: int) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def all_packages(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM packages ORDER BY price, id")
        return [dict(r) for r in await cur.fetchall()]

    async def update_package(self, pkg_id: int, **fields) -> None:
        allowed = ("name", "gb", "days", "monthly_gb", "price")
        cols = [c for c in allowed if c in fields]
        if not cols:
            return
        async with self._lock:
            await self.conn.execute(
                f"UPDATE packages SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [pkg_id],
            )
            await self.conn.commit()

    async def delete_package(self, pkg_id: int) -> None:
        # The ledger snapshots what was sold, so history survives this.
        async with self._lock:
            await self.conn.execute("DELETE FROM packages WHERE id = ?", (pkg_id,))
            await self.conn.commit()

    # credit ----------------------------------------------------------------
    async def charge(self, admin_id: int, amount: int, **entry) -> int | None:
        """Take `amount` off an admin's credit and return the ledger row id, or
        None if they cannot afford it. Never overdraws.

        The check IS the write: `WHERE credit >= ?` decides it inside SQLite, so
        two concurrent sales cannot both pass a separate `credit > 0` test and
        drive the balance negative. That holds no matter how many workers run,
        which a read-then-write guarded only by our asyncio lock would not.
        """
        async with self._lock:
            try:
                cur = await self.conn.execute(
                    "UPDATE admins SET credit = credit - ? "
                    "WHERE id = ? AND credit_enabled = 1 AND credit >= ?",
                    (int(amount), admin_id, int(amount)),
                )
                if cur.rowcount == 0:
                    await self.conn.rollback()
                    return None
                _bal, entry_id = await self._ledger(admin_id, -int(amount), **entry)
                await self.conn.commit()
                return entry_id
            except BaseException:
                await self.conn.rollback()
                raise

    async def credit_admin(self, admin_id: int, delta: int, **entry) -> int:
        """Give credit back (a reversal) or top up. Returns the new balance.

        Unconditional on purpose: a reversal must always land, or a failed sale
        would quietly keep the money.
        """
        async with self._lock:
            try:
                await self.conn.execute(
                    "UPDATE admins SET credit = credit + ? WHERE id = ?",
                    (int(delta), admin_id),
                )
                bal, _id = await self._ledger(admin_id, int(delta), **entry)
                await self.conn.commit()
                return bal
            except BaseException:
                await self.conn.rollback()
                raise

    async def _ledger(self, admin_id: int, delta: int, reason: str = "adjust",
                      package_id: int | None = None, package_name: str | None = None,
                      price_before_discount: int | None = None,
                      server_id: str | None = None, key_id: str | None = None,
                      note: str | None = None) -> tuple[int, int]:
        """Record one movement; returns (balance_after, row id). Caller holds
        the lock and commits."""
        cur = await self.conn.execute(
            "SELECT credit FROM admins WHERE id = ?", (admin_id,)
        )
        row = await cur.fetchone()
        bal = int(row["credit"] or 0) if row else 0
        cur = await self.conn.execute(
            "INSERT INTO credit_ledger (admin_id, delta, balance_after, reason,"
            " package_id, package_name, price_before_discount, server_id, key_id,"
            " note, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (admin_id, delta, bal, reason, package_id, package_name,
             price_before_discount, server_id, key_id, note, int(time.time())),
        )
        return bal, cur.lastrowid

    async def tag_ledger(self, entry_id: int, server_id: str, key_id: str) -> None:
        """Point a purchase at the key it bought. The charge happens before the
        key exists, so the statement would otherwise say what was sold but not
        to whom."""
        async with self._lock:
            await self.conn.execute(
                "UPDATE credit_ledger SET server_id = ?, key_id = ? WHERE id = ?",
                (server_id, key_id, entry_id),
            )
            await self.conn.commit()

    async def ledger_for(self, admin_id: int, limit: int = 200) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM credit_ledger WHERE admin_id = ?"
            " ORDER BY id DESC LIMIT ?", (admin_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def ledger_sum(self, admin_id: int) -> int:
        """The balance the ledger implies. Must equal admins.credit — the drift
        between them is the thing worth testing."""
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM credit_ledger WHERE admin_id = ?",
            (admin_id,),
        )
        return int((await cur.fetchone())["s"])

    # backup / restore ------------------------------------------------------
    _SERVER_COLS = ("id", "name", "api_url", "cert_sha256", "created_ts")
    _KEY_COLS = ("server_id", "key_id", "name", "limit_bytes", "duration_days",
                 "activated_ts", "expiry_ts", "disabled", "monthly_bytes",
                 "reset_ts", "sub_token", "created_ts", "owner_admin_id")
    _ADMIN_COLS = ("id", "username", "pw_hash", "pw_salt", "is_owner", "caps",
                   "servers", "disabled", "created_ts", "credit",
                   "credit_enabled", "discount_pct", "telegram_id")
    _PACKAGE_COLS = ("id", "name", "gb", "days", "monthly_gb", "price", "created_ts")
    _LEDGER_COLS = ("id", "admin_id", "delta", "balance_after", "reason",
                    "package_id", "package_name", "price_before_discount",
                    "server_id", "key_id", "note", "created_ts")

    async def export_all(self) -> dict:
        return {
            "version": 1,
            "servers": await self.all_servers(),
            "keys": await self.all_keys(),
            "settings": await self.all_settings(),
            "admins": await self.all_admins(),
            "packages": await self.all_packages(),
            "ledger": await self.all_ledger(),
        }

    async def all_ledger(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM credit_ledger ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def import_all(self, data: dict) -> None:
        """Replace servers/keys/settings with ``data``, all-or-nothing.

        The wipe and the refill MUST share one transaction: without the
        rollback, a bad row leaves the DELETEs pending and the next commit
        from anywhere (the scheduler) flushes them — losing the whole panel.
        """
        servers = data.get("servers") or []
        keys = data.get("keys") or []
        settings = data.get("settings") or {}
        admins = data.get("admins") or []
        packages = data.get("packages") or []
        ledger = data.get("ledger") or []
        async with self._lock:
            try:
                await self.conn.execute("DELETE FROM servers")
                await self.conn.execute("DELETE FROM keys")
                await self.conn.execute("DELETE FROM settings")
                await self.conn.execute("DELETE FROM admins")
                await self.conn.execute("DELETE FROM packages")
                await self.conn.execute("DELETE FROM credit_ledger")
                for s in servers:
                    cols = [c for c in self._SERVER_COLS if c in s]
                    await self.conn.execute(
                        f"INSERT INTO servers ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [s.get(c) for c in cols],
                    )
                for k in keys:
                    cols = [c for c in self._KEY_COLS if c in k]
                    await self.conn.execute(
                        f"INSERT INTO keys ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [k.get(c) for c in cols],
                    )
                for a in admins:
                    cols = [c for c in self._ADMIN_COLS if c in a]
                    await self.conn.execute(
                        f"INSERT INTO admins ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [a.get(c) for c in cols],
                    )
                for pk in packages:
                    cols = [c for c in self._PACKAGE_COLS if c in pk]
                    await self.conn.execute(
                        f"INSERT INTO packages ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [pk.get(c) for c in cols],
                    )
                for le in ledger:
                    cols = [c for c in self._LEDGER_COLS if c in le]
                    await self.conn.execute(
                        f"INSERT INTO credit_ledger ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [le.get(c) for c in cols],
                    )
                for key, val in settings.items():
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (key, val),
                    )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
