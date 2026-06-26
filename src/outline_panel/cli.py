"""
Management CLI (`outline-panel-admin`) for operators.

Useful when locked out of the panel: reset the admin password directly in the
DB, or print status.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass

from . import __version__, config
from .db import DB
from .settings import (
    BOT_ENABLED, BOT_TOKEN, TOTP_ENABLED, SettingsStore,
)


async def _reset_password(new: str | None) -> None:
    if not new:
        new = getpass.getpass("New admin password: ")
        if new != getpass.getpass("Confirm: "):
            raise SystemExit("Passwords did not match.")
    if len(new) < 6:
        raise SystemExit("Password must be at least 6 characters.")
    db = DB(config.DB_PATH)
    await db.init()
    try:
        await SettingsStore(db).set_admin_password(new)
    finally:
        await db.close()
    print("Admin password updated.")


async def _info() -> None:
    db = DB(config.DB_PATH)
    await db.init()
    try:
        s = SettingsStore(db)
        servers = await db.all_servers()
        keys = await db.all_keys()
        print(f"Outline Panel {__version__}")
        print(f"  DB:            {config.DB_PATH}")
        print(f"  Servers:       {len(servers)}")
        print(f"  Keys:          {len(keys)}")
        print(f"  Bot token set: {bool(await s.get(BOT_TOKEN))}")
        print(f"  Bot enabled:   {await s.get_bool(BOT_ENABLED)}")
        print(f"  2FA enabled:   {await s.get_bool(TOTP_ENABLED)}")
    finally:
        await db.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="outline-panel-admin",
                                description="Outline Panel management CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("reset-password", help="set the admin panel password")
    rp.add_argument("password", nargs="?", help="new password (prompted if omitted)")
    sub.add_parser("info", help="show panel status")
    args = p.parse_args()

    if args.cmd == "reset-password":
        asyncio.run(_reset_password(args.password))
    elif args.cmd == "info":
        asyncio.run(_info())


if __name__ == "__main__":
    main()
