"""
زمان‌بند پس‌زمینه برای همه‌ی سرورها:

۱) فعال‌سازی از اولین اتصال — کلیدهای دارای مدت که هنوز فعال نشده‌اند، اگر
   ترافیکی ازشان دیده شود (مصرف > ۰) فعال می‌شوند و انقضا = اکنون + مدت.
۲) ریست سهمیه‌ی ماهانه — کلیدهای دارای monthly_bytes، در زمان reset_ts سقف
   حجمشان تازه می‌شود (used + monthly_bytes) و reset بعدی ~۳۰ روز جلو می‌رود.
۳) اعلان‌ها (در صورت پاس‌دادن notifier) — نزدیک‌شدن به سقف حجم یا انقضا.
۴) اعمال انقضا — کلیدهای منقضی با ست‌کردن سقف حجم روی صفر غیرفعال می‌شوند.

`registry` باید متد get(server_id) داشته باشد که شیء OutlineAPI آن سرور
(یا None) را برمی‌گرداند. `notifier` یک کوروتین async است که یک رشته می‌گیرد.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import config
from .outline_api import OutlineError
from .utils import fmt_bytes, fmt_expiry

log = logging.getLogger("scheduler")

_MONTH_SECONDS = 30 * 86400


async def expiry_loop(registry, db, interval: int, notifier=None) -> None:
    # حافظه‌ی ضد-اسپم اعلان‌ها: مجموعه‌ی (sid, kid, kind)
    notified: set[tuple] = set()
    while True:
        try:
            await _check_once(registry, db, notifier, notified)
        except Exception as e:  # noqa: BLE001 — حلقه نباید بمیرد
            log.exception("خطا در زمان‌بند: %s", e)
        await asyncio.sleep(interval)


async def _safe_notify(notifier, text: str) -> None:
    if not notifier:
        return
    try:
        await notifier(text)
    except Exception as e:  # noqa: BLE001
        log.warning("ارسال اعلان ناموفق بود: %s", e)


async def _check_once(registry, db, notifier, notified) -> None:
    now = int(time.time())
    usage_cache: dict[str, dict] = {}

    async def usage(sid: str) -> dict:
        if sid not in usage_cache:
            api = registry.get(sid)
            try:
                usage_cache[sid] = await api.get_transfer_metrics() if api else {}
            except OutlineError as e:
                log.warning("خواندن مصرف سرور %s ناموفق بود: %s", sid, e)
                usage_cache[sid] = {}
        return usage_cache[sid]

    # ۱) فعال‌سازی از اولین اتصال
    for key in await db.pending_activation_keys():
        sid, kid = key["server_id"], key["key_id"]
        if registry.get(sid) is None:
            continue
        u = await usage(sid)
        if int(u.get(str(kid), 0)) > 0:
            expiry = now + int(key["duration_days"]) * 86400
            await db.activate(sid, kid, now, expiry)
            log.info("کلید %s/%s با اولین اتصال فعال شد.", sid, kid)

    # ۲) ریست سهمیه‌ی ماهانه + ۳) اعلان‌ها (روی همه‌ی کلیدها)
    limit_pct = config.NOTIFY_LIMIT_PERCENT / 100
    warn_window = config.NOTIFY_EXPIRY_DAYS * 86400
    for key in await db.all_keys():
        sid, kid = key["server_id"], key["key_id"]
        api = registry.get(sid)
        if api is None:
            continue
        name = key.get("name") or kid

        # ریست ماهانه
        mb, rt = key.get("monthly_bytes"), key.get("reset_ts")
        if mb and rt and now >= rt and not key.get("disabled"):
            used = int((await usage(sid)).get(str(kid), 0))
            new_limit = used + int(mb)
            try:
                await api.set_data_limit(kid, new_limit)
                await db.set_limit(sid, kid, new_limit)
                # reset بعدی را از rt جلو می‌بریم تا از دریفت جلوگیری شود
                nxt = rt
                while nxt <= now:
                    nxt += _MONTH_SECONDS
                await db.set_reset(sid, kid, nxt)
                notified.discard((sid, kid, "limit"))
                log.info("سهمیه‌ی ماهانه‌ی %s/%s ریست شد (%s).", sid, kid, fmt_bytes(mb))
                await _safe_notify(
                    notifier,
                    f"🔄 سهمیه‌ی ماهانه‌ی <b>{name}</b> تازه شد ({fmt_bytes(mb)}).",
                )
            except OutlineError as e:
                log.warning("ریست ماهانه‌ی %s/%s ناموفق بود: %s", sid, kid, e)
            key = await db.get_key(sid, kid) or key  # مقادیر به‌روز

        if not notifier or key.get("disabled"):
            continue

        # اعلان نزدیک‌شدن به سقف حجم
        lim = key.get("limit_bytes")
        tag_lim = (sid, kid, "limit")
        if lim:
            used = int((await usage(sid)).get(str(kid), 0))
            if used >= lim * limit_pct:
                if tag_lim not in notified:
                    notified.add(tag_lim)
                    await _safe_notify(
                        notifier,
                        f"⚠️ مصرف <b>{name}</b> به {fmt_bytes(used)} از "
                        f"{fmt_bytes(lim)} رسید.",
                    )
            else:
                notified.discard(tag_lim)
        else:
            notified.discard(tag_lim)

        # اعلان نزدیک‌شدن به انقضا
        exp = key.get("expiry_ts")
        tag_exp = (sid, kid, "expiry")
        if exp and 0 < exp - now <= warn_window:
            if tag_exp not in notified:
                notified.add(tag_exp)
                await _safe_notify(
                    notifier,
                    f"⏳ اعتبار <b>{name}</b> رو به پایان است: {fmt_expiry(exp)}.",
                )
        else:
            notified.discard(tag_exp)

    # ۴) اعمال انقضا
    for key in await db.expired_active_keys(now):
        sid, kid = key["server_id"], key["key_id"]
        api = registry.get(sid)
        if api is None:
            continue
        try:
            await api.set_data_limit(kid, 0)
            await db.set_disabled(sid, kid, True)
            log.info("کلید %s/%s به دلیل انقضا غیرفعال شد.", sid, kid)
            await _safe_notify(
                notifier,
                f"🔴 کلید <b>{key.get('name') or kid}</b> به دلیل انقضا غیرفعال شد.",
            )
        except OutlineError as e:
            log.warning("غیرفعال‌سازی %s/%s ناموفق بود: %s", sid, kid, e)
