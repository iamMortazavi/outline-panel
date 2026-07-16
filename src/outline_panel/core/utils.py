"""Shared formatting and unit helpers used by the bot and the scheduler."""

from __future__ import annotations

from datetime import datetime, timezone

GB = 1024 ** 3
# One billing cycle. The router seeds the first reset with it and the
# scheduler advances every later one — they must not drift apart.
MONTH_SECONDS = 30 * 86400


def gb_to_bytes(gb: float) -> int:
    return int(gb * GB)


def fmt_bytes(n: int | None) -> str:
    """Human-readable byte size ('Unlimited' when None)."""
    if n is None:
        return "Unlimited"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def fmt_expiry(expiry_ts: int | None) -> str:
    if not expiry_ts:
        return "No expiry"
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    remaining = expiry_ts - now.timestamp()
    if remaining <= 0:
        return f"Expired ({dt:%Y-%m-%d})"
    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    return f"{dt:%Y-%m-%d %H:%M} UTC ({days}d {hours}h left)"
