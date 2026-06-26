"""توابع کمکی مشترک."""

from __future__ import annotations

from datetime import datetime, timezone

GB = 1024 ** 3


def gb_to_bytes(gb: float) -> int:
    return int(gb * GB)


def fmt_bytes(n: int | None) -> str:
    """نمایش خوانای حجم."""
    if n is None:
        return "نامحدود"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def fmt_expiry(expiry_ts: int | None) -> str:
    if not expiry_ts:
        return "بدون انقضا"
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    remaining = expiry_ts - now.timestamp()
    if remaining <= 0:
        return f"منقضی شده ({dt:%Y-%m-%d})"
    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    return f"{dt:%Y-%m-%d %H:%M} UTC ({days} روز و {hours} ساعت مانده)"
