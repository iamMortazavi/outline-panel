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
    PRIMARY KEY (server_id, key_id)
);
"""

_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
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
        # runtime settings table (password hash, bot token, 2FA, ...)
        await self._db.execute(_SETTINGS_SCHEMA)
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
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO keys "
                "(server_id, key_id, name, limit_bytes, duration_days, "
                " activated_ts, expiry_ts, disabled, created_ts) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?)",
                (server_id, key_id, name, limit_bytes, duration_days, int(time.time())),
            )
            await self.conn.commit()

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

    # backup / restore ------------------------------------------------------
    _SERVER_COLS = ("id", "name", "api_url", "cert_sha256", "created_ts")
    _KEY_COLS = ("server_id", "key_id", "name", "limit_bytes", "duration_days",
                 "activated_ts", "expiry_ts", "disabled", "monthly_bytes",
                 "reset_ts", "sub_token", "created_ts")

    async def export_all(self) -> dict:
        return {
            "version": 1,
            "servers": await self.all_servers(),
            "keys": await self.all_keys(),
            "settings": await self.all_settings(),
        }

    async def import_all(self, data: dict) -> None:
        servers = data.get("servers", [])
        keys = data.get("keys", [])
        settings = data.get("settings", {})
        async with self._lock:
            await self.conn.execute("DELETE FROM servers")
            await self.conn.execute("DELETE FROM keys")
            await self.conn.execute("DELETE FROM settings")
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
            for key, val in settings.items():
                await self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, val),
                )
            await self.conn.commit()
