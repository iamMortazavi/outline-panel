"""
نمایش فقط‌خواندنی یوزرهای فعلی سرور Outline.

این اسکریپت فقط درخواست‌های GET می‌زند: لیست کلیدها و میزان مصرف.
هیچ کلیدی ساخته، حذف یا تغییر داده نمی‌شود.

اجرا:
    python3 list_users.py
"""

from __future__ import annotations

import asyncio

from . import config
from .outline_api import OutlineAPI


def gb(b: int | None) -> str:
    return "—" if b is None else f"{b / 1024**3:.2f} GB"


async def main() -> None:
    api = OutlineAPI(config.require_api_url(), config.OUTLINE_CERT_SHA256, timeout=20)
    try:
        info = await api.get_server_info()
        print(
            f"Server: {info.get('name')} | version {info.get('version')} "
            f"| default port {info.get('portForNewAccessKeys')}"
        )
        print(f"Global data limit: {info.get('accessKeyDataLimit')}\n")

        keys = await api.list_keys()              # GET only
        usage = await api.get_transfer_metrics()  # GET only

        print(f"Total users: {len(keys)}\n")
        print(f"{'ID':>4}  {'Name':<28} {'Used':>11}  {'Limit':>11}")
        print("-" * 60)
        for k in sorted(
            keys, key=lambda x: int(x["id"]) if str(x["id"]).isdigit() else 0
        ):
            kid = k["id"]
            used = usage.get(str(kid), 0)
            limit = k.get("dataLimit", {}).get("bytes")
            name = (k.get("name") or "(no name)")[:28]
            print(f"{kid:>4}  {name:<28} {gb(used):>11}  {gb(limit):>11}")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
