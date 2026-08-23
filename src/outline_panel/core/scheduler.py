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
import os
import socket
import time
import uuid

from . import backup, config, metrics
from .outline_api import OutlineError
from .utils import MONTH_SECONDS, fmt_bytes, fmt_expiry

log = logging.getLogger("scheduler")



LEASE = "scheduler"


async def expiry_loop(registry, db, interval: int, notifier=None,
                      settings=None, holder: str | None = None) -> None:
    """Run the checks forever, in exactly one process.

    Coordination used to be the `ENABLE_SCHEDULER` env var: the operator had to
    know to switch it off wherever a second process ran, and getting it wrong
    meant two schedulers resetting quotas and expiring the same keys against
    each other. A lease settles it instead — every process runs this loop, only
    the lease-holder does the work, and if that one dies the lease expires and
    another picks it up within a cycle.

    `settings` makes the interval and thresholds live: re-read every pass, so a
    change in the panel applies without a restart.
    """
    # Distinct per process. pid alone repeats across containers.
    holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    # anti-spam memory for notifications: a set of (sid, kid, kind)
    notified: set[tuple] = set()
    held = False
    while True:
        nap = interval
        if settings is not None:
            try:
                nap = await settings.num("expiry_check_interval")
            except Exception as e:  # noqa: BLE001 — never let a read kill the loop
                log.warning("could not read the scheduler interval: %s", e)
        try:
            # Long enough that a slow pass cannot lose the lease mid-run, short
            # enough that a dead holder is replaced promptly.
            if await db.acquire_lease(LEASE, holder, max(30, nap * 3)):
                if not held:
                    log.info("scheduler lease acquired by %s", holder)
                    held = True
                metrics.observe("outline_panel_scheduler_leader", 1)
                await _check_once(registry, db, notifier, notified, settings)
                metrics.inc("outline_panel_scheduler_passes_total")
            elif held:
                log.info("scheduler lease lost; standing by")
                held = False
                metrics.observe("outline_panel_scheduler_leader", 0)
        except Exception as e:  # noqa: BLE001 — the loop must never die
            log.exception("scheduler error: %s", e)
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


async def _probe_servers(registry, db, notifier, notified, settings) -> None:
    """Ask every server whether it is alive, and remember the answer.

    Alerts fire on a *run* of failures, not on one: a single missed probe is a
    blip, and paging on blips is how people learn to ignore the alert. The
    recovery notice is tied to the same `notified` set, so it can only arrive
    after an alert actually went out.
    """
    threshold = await settings.num("health_alert_failures")
    for sid in registry.ids() if hasattr(registry, "ids") else []:
        api = registry.get(sid)
        if api is None:
            continue
        name = (registry.meta(sid) or {}).get("name") or sid
        started = time.monotonic()
        try:
            await api.get_server_info()
            reachable, err = True, None
        except OutlineError as e:
            reachable, err = False, str(e)
        latency = int((time.monotonic() - started) * 1000)
        await db.record_health(sid, reachable, latency if reachable else None, err)

        tag = (sid, None, "down")
        if not reachable:
            if await db.consecutive_failures(sid) >= threshold and tag not in notified:
                notified.add(tag)
                await _safe_notify(
                    notifier,
                    f"🔌 <b>{name}</b> has failed {threshold} checks in a row "
                    f"and looks down.\n<code>{(err or '')[:120]}</code>",
                )
        elif tag in notified:
            notified.discard(tag)
            await _safe_notify(notifier, f"✅ <b>{name}</b> is reachable again.")


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

    # 3b) probe every server and remember the answer
    if settings is not None:
        try:
            await _probe_servers(registry, db, notifier, notified, settings)
        except Exception as e:  # noqa: BLE001
            log.warning("health probe failed: %s", e)

    # 4) housekeeping: these tables only ever grow, so something has to trim them
    if settings is not None:
        try:
            cutoff = now - await settings.num("audit_retention_days") * 86400
            gone = await db.prune_audit(cutoff)
            if gone:
                log.info("pruned %d audit entries older than %d days.", gone,
                         await settings.num("audit_retention_days"))
            # A day is far longer than any client will retry; keeping the rows
            # past that only preserves the chance of replaying a stale purchase.
            await db.prune_idempotency(now - 86400)
            await db.prune_rate_events(now - 86400)
            await db.prune_health(now - await settings.num("health_retention_days") * 86400)
        except Exception as e:  # noqa: BLE001 — housekeeping must not stop expiry
            log.warning("housekeeping failed: %s", e)
        try:
            await backup.maybe_backup(db, settings, db.path, now)
        except Exception as e:  # noqa: BLE001 — a full disk must not stop expiry
            log.warning("backup failed: %s", e)

    # 4a) convergence: retry the effects that never reached their server, and
    # (only when switched on) close the gap the A1 defect left behind. Both are
    # the lease holder's job, so N workers do not race the same retries.
    if settings is not None:
        try:
            from ..application import convergence
            summary = await convergence.drain(db, registry, now)
            if summary["applied"] or summary["failed"]:
                log.info("outbox: %(applied)d applied, %(failed)d still failing, "
                         "%(dropped)d dropped", summary)
            metrics.observe("outline_panel_outbox_depth", await db.outbox_depth())
            if await settings.get_bool("reconcile_enabled", False):
                result = await convergence.reconcile(db, registry, apply=True)
                if result["queued"]:
                    log.warning("reconciliation queued %d suspensions that had "
                                "not reached their server", result["queued"])
        except Exception as e:  # noqa: BLE001 — convergence must not stop expiry
            log.warning("convergence pass failed: %s", e)

    # 4b) reconciliation: admins.credit is what a purchase is checked against,
    # the ledger is how it got there. They must agree, and nothing was watching.
    try:
        for row in await db.credit_drift():
            log.error("CREDIT DRIFT: admin %s (%s) holds %s but the ledger says %s",
                      row["id"], row["username"], row["credit"], row["ledger"])
            await _safe_notify(
                notifier,
                f"🚨 Credit mismatch for <b>{row['username']}</b>: balance "
                f"{row['credit']:,} but the statement totals {row['ledger']:,}. "
                f"No money has been lost — but something wrote the balance "
                f"outside the ledger, so please check before selling more.",
            )
    except Exception as e:  # noqa: BLE001
        log.warning("reconciliation failed: %s", e)

    # 5) enforce expiry
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
