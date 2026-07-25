"""
Background scheduler for all servers:

1) Activation on first connection — keys with a duration that aren't active yet
   are activated once traffic is seen (usage > 0); expiry = now + duration.
2) Monthly quota reset — keys with monthly_bytes get their limit refreshed at
   reset_ts (used + monthly_bytes), and the next reset moves ~30 days forward.
3) Notifications (when a notifier is provided) — nearing the data limit or expiry.
4) Expiry enforcement — expired keys are disabled by setting their limit to zero.

`registry` must expose get(server_id) returning that server's OutlineAPI (or
None). `notifier` is an async coroutine that takes a single string.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import config
from .outline_api import OutlineError
from .utils import MONTH_SECONDS, fmt_bytes, fmt_expiry

log = logging.getLogger("scheduler")



async def expiry_loop(registry, db, interval: int, notifier=None,
                      settings=None) -> None:
    """Run the checks forever.

    `settings` (a SettingsStore) makes the interval and the notification
    thresholds live: they are re-read every pass, so changing them in the panel
    takes effect without a restart. `interval` remains the fallback for callers
    that have no store.
    """
    # anti-spam memory for notifications: a set of (sid, kid, kind)
    notified: set[tuple] = set()
    while True:
        try:
            await _check_once(registry, db, notifier, notified, settings)
        except Exception as e:  # noqa: BLE001 — the loop must never die
            log.exception("scheduler error: %s", e)
        nap = interval
        if settings is not None:
            try:
                nap = await settings.num("expiry_check_interval")
            except Exception as e:  # noqa: BLE001 — never let a read kill the loop
                log.warning("could not read the scheduler interval: %s", e)
        await asyncio.sleep(nap)


async def _safe_notify(notifier, text: str, key: dict | None = None) -> None:
    """Send one alert to whoever owns `key`.

    `owner_admin_id` is passed positionally-by-name so an older notifier that
    only takes `text` (the tests', and any custom one) keeps working.
    """
    if not notifier:
        return
    try:
        if key is None:
            await notifier(text)
        else:
            await notifier(text, owner_admin_id=key.get("owner_admin_id"))
    except TypeError:  # notifier that only accepts a message
        try:
            await notifier(text)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to send notification: %s", e)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to send notification: %s", e)


async def _check_once(registry, db, notifier, notified, settings=None) -> None:
    now = int(time.time())
    if hasattr(registry, "sync"):   # pick up servers added since the last pass
        await registry.sync()
    # Thresholds come from the panel when a store is available, from env
    # otherwise — the same value either way on a panel nobody has retuned.
    if settings is not None:
        limit_pct = await settings.num("notify_limit_percent") / 100
        warn_window = await settings.num("notify_expiry_days") * 86400
        cycle_seconds = await settings.cycle_seconds()
    else:
        limit_pct = config.NOTIFY_LIMIT_PERCENT / 100
        warn_window = config.NOTIFY_EXPIRY_DAYS * 86400
        cycle_seconds = MONTH_SECONDS
    usage_cache: dict[str, dict] = {}

    async def usage(sid: str) -> dict:
        if sid not in usage_cache:
            api = registry.get(sid)
            try:
                usage_cache[sid] = await api.get_transfer_metrics() if api else {}
            except OutlineError as e:
                log.warning("failed to read usage for server %s: %s", sid, e)
                usage_cache[sid] = {}
        return usage_cache[sid]

    # 1) activation on first connection
    for key in await db.pending_activation_keys():
        sid, kid = key["server_id"], key["key_id"]
        if registry.get(sid) is None:
            continue
        u = await usage(sid)
        if int(u.get(str(kid), 0)) > 0:
            expiry = now + int(key["duration_days"]) * 86400
            await db.activate(sid, kid, now, expiry)
            log.info("key %s/%s activated on first connection.", sid, kid)

    # 2) monthly quota reset + 3) notifications (across all keys)
    for key in await db.all_keys():
        sid, kid = key["server_id"], key["key_id"]
        api = registry.get(sid)
        if api is None:
            continue
        name = key.get("name") or kid

        # monthly reset
        mb, rt = key.get("monthly_bytes"), key.get("reset_ts")
        if mb and rt and now >= rt and not key.get("disabled"):
            used = int((await usage(sid)).get(str(kid), 0))
            new_limit = used + int(mb)
            try:
                await api.set_data_limit(kid, new_limit)
                await db.set_limit(sid, kid, new_limit)
                # advance the next reset from rt to avoid drift
                nxt = rt
                while nxt <= now:
                    nxt += cycle_seconds
                await db.set_reset(sid, kid, nxt)
                notified.discard((sid, kid, "limit"))
                log.info("monthly quota for %s/%s reset (%s).", sid, kid, fmt_bytes(mb))
                await _safe_notify(
                    notifier,
                    f"🔄 Monthly quota for <b>{name}</b> has been reset "
                    f"({fmt_bytes(mb)}).",
                    key,
                )
            except OutlineError as e:
                log.warning("monthly reset for %s/%s failed: %s", sid, kid, e)
            key = await db.get_key(sid, kid) or key  # refreshed values

        if not notifier or key.get("disabled"):
            continue

        # data-limit warning
        lim = key.get("limit_bytes")
        tag_lim = (sid, kid, "limit")
        if lim:
            used = int((await usage(sid)).get(str(kid), 0))
            if used >= lim * limit_pct:
                if tag_lim not in notified:
                    notified.add(tag_lim)
                    await _safe_notify(
                        notifier,
                        f"⚠️ <b>{name}</b> has used {fmt_bytes(used)} of "
                        f"{fmt_bytes(lim)}.",
                        key,
                    )
            else:
                notified.discard(tag_lim)
        else:
            notified.discard(tag_lim)

        # expiry warning
        exp = key.get("expiry_ts")
        tag_exp = (sid, kid, "expiry")
        if exp and 0 < exp - now <= warn_window:
            if tag_exp not in notified:
                notified.add(tag_exp)
                await _safe_notify(
                    notifier,
                    f"⏳ <b>{name}</b> is about to expire: {fmt_expiry(exp)}.",
                    key,
                )
        else:
            notified.discard(tag_exp)

    # 4) enforce expiry
    for key in await db.expired_active_keys(now):
        sid, kid = key["server_id"], key["key_id"]
        api = registry.get(sid)
        if api is None:
            continue
        try:
            await api.set_data_limit(kid, 0)
            await db.set_disabled(sid, kid, True)
            log.info("key %s/%s disabled (expired).", sid, kid)
            await _safe_notify(
                notifier,
                f"🔴 <b>{key.get('name') or kid}</b> has been disabled (expired).",
                key,
            )
        except OutlineError as e:
            log.warning("failed to disable %s/%s: %s", sid, kid, e)
