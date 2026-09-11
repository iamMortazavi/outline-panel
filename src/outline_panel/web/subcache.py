"""The subscription summary cache.

Its own module because two sides need it and neither should own it: the public
subscription route reads and fills it, and the key routes have to drop an entry
the moment they change who is in a subscription. It lived in the subscription
router, so the key service reached across into a router to invalidate — and a
router importing a router is how the import cycle in this package started.

Process-local and deliberately so: this is a load shield, not a source of truth,
and a worker serving a copy a few seconds older than its neighbour's costs
nothing. Bounded by ``_CACHE_MAX`` so a flood of invented tokens cannot grow it —
misses raise 404 before ever landing here.

Usage figures going a few seconds stale is cosmetic — suspending a user sets
their Outline data limit to zero, which cuts the tunnel immediately whatever
this says. *Membership* is not cosmetic: unlinking a server has to stop that
config being handed out, which is what `invalidate` is for.
"""

from __future__ import annotations

import time

# token -> (expires_at, summary)
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 5000


def get(token: str) -> dict | None:
    hit = _cache.get(token)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def put(token: str, info: dict, ttl: float) -> None:
    if not ttl:
        return
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[token] = (time.monotonic() + ttl, info)


def invalidate(token: str | None) -> None:
    """Drop a cached summary. Called wherever a subscription's membership
    changes, so a removed server stops being served at once."""
    if token:
        _cache.pop(token, None)
