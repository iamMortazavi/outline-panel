"""Access-key (user) management across all servers."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core import errors, metrics, security
from ...core.outline_api import OutlineAPI, OutlineError
from ...core.utils import gb_to_bytes
from .. import idempotency
from ..deps import (
    api_or_404,
    can_see,
    db,
    enforce_scope,
    is_owner,
    on_credit,
    owns,
    price_for,
    reg,
    require,
    require_owner,
    scoped_ids,
    settings,
    sids_or_404,
)
from . import subscription as sub_router

log = logging.getLogger("web.keys")
router = APIRouter(prefix="/api", tags=["keys"],
                   dependencies=[Depends(enforce_scope)])


# --------------------------------------------------------------- read helpers
async def _conn_info(api: OutlineAPI) -> dict[str, dict]:
    try:
        m = await api.get_server_metrics_cached("30d", await settings.num("metrics_ttl"))
    except OutlineError:
        return {}
    conn = {}
    for ak in m.get("accessKeys", []):
        c = ak.get("connection", {}) or {}
        conn[str(ak.get("accessKeyId"))] = {
            "lastSeen": c.get("lastTrafficSeen"),
            "peakDevices": (c.get("peakDeviceCount") or {}).get("data"),
            "tunnelSec": (ak.get("tunnelTime") or {}).get("seconds"),
        }
    return conn


async def keys_for_server(sid: str, admin: dict, names: dict) -> dict:
    m = reg.meta(sid)
    if m is None:  # server removed between snapshot and fetch
        return {"serverId": sid, "serverName": None, "keys": [], "error": "Server removed"}
    api = m["api"]
    try:
        # Run the three upstream reads concurrently on the reused connection
        # pool; _conn_info swallows its own errors, so only list_keys /
        # get_transfer_metrics raising OutlineError lands in the except below.
        keys, usage, conn = await asyncio.gather(
            api.list_keys(), api.get_transfer_metrics(), _conn_info(api)
        )
    except OutlineError as e:
        # Server briefly unreachable — surface the error, don't drop its keys.
        return {"serverId": sid, "serverName": m["name"], "keys": [], "error": str(e)}
    local = {k["key_id"]: k for k in await db.keys_for(sid)}
    base = await settings.get_profile_base()
    out = []
    for k in keys:
        kid = k["id"]
        meta = local.get(kid)
        # A sub-admin's page is their own customers only. Two resellers on one
        # server must not see, edit or delete each other's users.
        if not owns(admin, meta):
            continue
        meta = meta or {}
        c = conn.get(str(kid), {})
        # Disabled keys report dataLimit=0 on Outline; show the stored limit.
        if meta.get("disabled"):
            limit_b = meta.get("limit_bytes")
        else:
            limit_b = k.get("dataLimit", {}).get("bytes")
            if limit_b is None:
                limit_b = meta.get("limit_bytes")
        duration = meta.get("duration_days")
        activated = meta.get("activated_ts") is not None
        out.append({
            "id": kid, "serverId": sid, "serverName": m["name"],
            "name": k.get("name") or f"Key {kid}",
            "accessUrl": k.get("accessUrl"),
            "used": int(usage.get(str(kid), 0)),
            "limit": limit_b,
            "expiry": meta.get("expiry_ts"),
            "monthlyBytes": meta.get("monthly_bytes"),
            "createdTs": meta.get("created_ts"),
            "ownerAdminId": meta.get("owner_admin_id"),
            "ownerName": names.get(meta.get("owner_admin_id")) or names.get(None),
            "durationDays": duration,
            "activated": activated,
            "pending": duration is not None and not activated,
            "disabled": bool(meta.get("disabled")),
            "subToken": meta.get("sub_token"),
            # Absolute when a profile host is configured; otherwise a path the
            # browser resolves against the panel's own origin.
            "profileUrl": (f"{base}/{meta['sub_token']}" if base and meta.get("sub_token")
                           else (f"/sub/{meta['sub_token']}" if meta.get("sub_token") else None)),
            "lastSeen": c.get("lastSeen"),
            "peakDevices": c.get("peakDevices"),
            "tunnelSec": c.get("tunnelSec"),
        })
    return {"serverId": sid, "serverName": m["name"], "keys": out, "error": None}


@router.get("/keys")
async def list_keys(server: str | None = None,
                   admin: dict = Depends(require("keys.view"))):
    sids = sids_or_404(server, admin)
    # one lookup for the whole list rather than per key
    names = {a["id"]: a["username"] for a in await db.all_admins()}
    owner = await db.get_owner()
    names[None] = owner["username"] if owner else "owner"
    results = await asyncio.gather(*[keys_for_server(s, admin, names) for s in sids])
    keys = [k for r in results for k in r["keys"]]
    keys.sort(key=lambda x: (x["serverName"] or "", int(x["id"]) if str(x["id"]).isdigit() else 0))
    errors = [
        {"serverId": r["serverId"], "serverName": r["serverName"], "error": r["error"]}
        for r in results if r["error"]
    ]
    return {"keys": keys, "errors": errors}


# --------------------------------------------------------------- write helpers
async def ensure_local(sid: str, kid: str) -> dict:
    """The local row for a key, creating one if the panel has not seen it.

    This is the adoption path: a key made straight from Outline Manager has no
    row here until someone edits it. It gets a customer link at the same moment,
    for the same reason a newly created key does — otherwise adopted keys are a
    quiet second class with no page to send anyone to.
    """
    meta = await db.get_key(sid, kid)
    if not meta:
        await db.add_key(sid, kid, "", None, None)
        await db.set_sub_token(sid, kid, security.profile_token(kid))
        meta = await db.get_key(sid, kid)
    return meta


async def enable_on_outline(api: OutlineAPI, kid: str, meta: dict) -> None:
    if meta and meta.get("limit_bytes") is not None:
        await api.set_data_limit(kid, int(meta["limit_bytes"]))
    else:
        await api.remove_data_limit(kid)


# --------------------------------------------------------------------- models
class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    limit_gb: float = Field(ge=0, default=0)
    days: int = Field(ge=0, default=0)
    monthly_gb: float = Field(ge=0, default=0)
    # False: the countdown waits for the user's first connection (the default,
    # so an unused key isn't burning its validity). True: it starts right now.
    start_now: bool = False
    # Credit-enabled admins must buy a package; the fields above are then
    # ignored, since the package decides what the user gets.
    package_id: int | None = None


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LimitBody(BaseModel):
    limit_gb: float = Field(ge=0)


class MonthlyBody(BaseModel):
    monthly_gb: float = Field(ge=0)


class ExtendBody(BaseModel):
    # positive extends validity (and re-enables); negative shortens it.
    days: int = Field(default=0, ge=-3650, le=3650)
    # A credit admin renews by buying another package instead of naming days.
    package_id: int | None = None


async def _buy(admin: dict, package_id: int | None, sid: str,
               kid: str | None = None) -> tuple[dict, int, int] | None:
    """Charge `admin` for a package, or raise. Returns (package, price, entry id).

    Returns None when the caller is not on credit, meaning "no purchase, use
    the free-form path". Reserving BEFORE the Outline call is deliberate: the
    caller must reverse it if the call fails (see _reverse), because a check
    that does not also take the money lets two tabs both pass it.
    """
    if not on_credit(admin):
        return None
    if package_id is None:
        raise errors.pick_a_package()
    pkg = await db.get_package(package_id)
    if pkg is None:
        raise errors.unknown_package()
    price = price_for(pkg, admin)
    entry = await db.charge(admin["id"], price, reason="purchase",
                            package_id=pkg["id"], package_name=pkg["name"],
                            price_before_discount=int(pkg["price"]),
                            server_id=sid, key_id=kid)
    if entry is None:
        raise errors.not_enough_credit(
            pkg["name"], price, int(admin.get("credit") or 0),
            await settings.text("currency"))
    return pkg, price, entry


def deny_free(admin: dict) -> None:
    """Refuse a route that hands a customer time or volume outside the price list.

    `extend_key` already enforces this through `_buy` — "any time or volume that
    reaches a user is paid for". Raising the data limit, granting a monthly
    quota, resetting usage or mirroring a subscription onto a second server all
    reach a user just as surely, and all four were free: a reseller bought the
    cheapest package and then topped it up here for nothing.
    """
    if on_credit(admin):
        raise errors.buy_a_package()


async def _apply_package(sid: str, kid: str, pkg: dict) -> dict:
    """Add a package's time and volume to an existing key (a renewal).

    limit_bytes is the *cumulative ceiling* Outline counts against, never a
    plan size, so adding to it is the correct operation. Outline is told first
    and the DB committed after — a 502 must not move the dates, or the retry
    charges twice and extends twice.
    """
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)
    now = int(time.time())

    cur_limit = meta.get("limit_bytes")
    if pkg["gb"] is None or cur_limit is None:
        new_limit = None          # unlimited either way; never take away access
    else:
        new_limit = int(cur_limit) + gb_to_bytes(pkg["gb"])
    try:
        if new_limit is None:
            await api.remove_data_limit(kid)
        else:
            await api.set_data_limit(kid, new_limit)
    except OutlineError as e:
        raise errors.upstream(str(e))

    await db.set_limit(sid, kid, new_limit)
    days = int(pkg["days"] or 0)
    if days:
        if meta.get("duration_days") is not None and meta.get("activated_ts") is None:
            # still pending: the clock has not started, so lengthen the term
            await db.set_duration(sid, kid, int(meta["duration_days"]) + days)
        else:
            base = max(meta.get("expiry_ts") or 0, now)
            await db.set_expiry(sid, kid, base + days * 86400)
    if meta.get("disabled"):
        await db.set_disabled(sid, kid, False)
    return {"ok": True, "limit": new_limit}


async def _reverse(admin: dict, bought: tuple[dict, int, int] | None, sid: str,
                   note: str) -> None:
    """Give back a charge for a sale that did not happen.

    "No refunds" is about an admin deleting a user they sold. Nothing was
    bought here, so keeping the money would just be an error.
    """
    if not bought:
        return
    pkg, price, _entry = bought
    try:
        await db.credit_admin(admin["id"], price, reason="reversal",
                              package_id=pkg["id"], package_name=pkg["name"],
                              server_id=sid, note=note)
    except Exception:  # noqa: BLE001 — never mask the original failure
        log.exception("could not reverse a charge for admin %s", admin["id"])


# --------------------------------------------------------------------- routes
async def create_key_for(sid: str, name: str, limit_gb: float, days: int,
                         monthly_gb: float = 0, start_now: bool = False,
                         owner_admin_id: int | None = None) -> dict:
    """Create a key on Outline and persist its local metadata.

    Shared by the dashboard route and the Telegram Mini App. Raises
    ``HTTPException`` on failure and removes any orphan key left on the server.

    ``start_now`` picks when the validity clock starts: at creation, or (the
    default) on the user's first connection, which the scheduler detects.
    """
    api = api_or_404(sid)
    limit_bytes = gb_to_bytes(limit_gb) if limit_gb > 0 else None
    monthly_bytes = gb_to_bytes(monthly_gb) if monthly_gb > 0 else None
    if monthly_bytes and limit_bytes is None:
        limit_bytes = monthly_bytes
    duration = days if days > 0 else None
    try:
        key = await api.create_key(name=name, limit_bytes=limit_bytes)
    except OutlineError as e:
        raise errors.upstream(str(e))
    try:
        await db.add_key(sid, key["id"], name, limit_bytes, duration,
                         owner_admin_id=owner_admin_id)
        now = int(time.time())
        if duration and start_now:
            # Activating here is all it takes: the scheduler only adopts keys
            # whose activated_ts is still NULL, so it leaves this one alone and
            # the normal expiry sweep does the rest.
            await db.activate(sid, key["id"], now, now + duration * 86400)
        if monthly_bytes:
            await db.set_monthly(sid, key["id"], monthly_bytes, now + await settings.cycle_seconds())
        # Issued here rather than on demand: a link the reseller has to remember
        # to generate is a link most customers never receive.
        await db.set_sub_token(sid, key["id"], security.profile_token(key["id"]))
    except Exception as e:  # noqa: BLE001 — avoid an orphan key on the server
        log.exception("DB persist failed; deleting orphan key %s", key.get("id"))
        try:
            await api.delete_key(key["id"])
        except OutlineError:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to persist key: {e}")
    metrics.inc("outline_panel_keys_created_total", {"server": sid})
    base = await settings.get_profile_base()
    tok = (await db.get_key(sid, key["id"]) or {}).get("sub_token")
    return {"id": key["id"], "serverId": sid, "name": name,
            "subToken": tok,
            "profileUrl": (f"{base}/{tok}" if base and tok
                           else (f"/sub/{tok}" if tok else None)),
            "accessUrl": key["accessUrl"], "limit": limit_bytes,
            "monthlyBytes": monthly_bytes, "createdTs": now,
            "durationDays": duration,
            "pending": duration is not None and not start_now}


@router.post("/servers/{sid}/keys")
async def create_key(sid: str, request: Request, body: CreateBody,
                     admin: dict = Depends(require("keys.create"))):
    # The charge lands before Outline is called, so a lost response used to
    # leave the admin paying for a retry. An Idempotency-Key replays the first
    # answer instead.
    guard = await idempotency.begin(request, admin)
    if guard.replay:
        return guard.replay
    mine = None if is_owner(admin) else admin["id"]
    try:
        bought = await _buy(admin, body.package_id, sid)
        if not bought:
            key = await create_key_for(sid, body.name, body.limit_gb, body.days,
                                       body.monthly_gb, body.start_now, mine)
            await guard.done(key)
            return key
        pkg, _price, entry = bought
        try:
            # The package decides what the user gets — the body's own limit/days
            # are ignored rather than merged, or the buyer picks their own size.
            key = await create_key_for(sid, body.name, pkg["gb"] or 0,
                                       pkg["days"] or 0, pkg["monthly_gb"] or 0,
                                       body.start_now, mine)
        except BaseException as e:
            await _reverse(admin, bought, sid, f"create failed: {e}")
            raise
    except BaseException:
        # Every failure above either charged nothing or reversed it, so the key
        # is free to be retried.
        await guard.abandon()
        raise
    # now that the key exists, point the charge at what it bought
    await db.tag_ledger(entry, sid, key["id"])
    await guard.done(key)
    return key


@router.put("/servers/{sid}/keys/{kid}/name", dependencies=[Depends(require("keys.edit"))])
async def rename_key(sid: str, kid: str, body: NameBody):
    api = api_or_404(sid)
    try:
        await api.rename_key(kid, body.name)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await ensure_local(sid, kid)
    await db.set_name(sid, kid, body.name)
    return {"ok": True}


@router.put("/servers/{sid}/keys/{kid}/limit")
async def set_key_limit(sid: str, kid: str, body: LimitBody,
                        admin: dict = Depends(require("keys.edit"))):
    deny_free(admin)
    api = api_or_404(sid)
    limit_bytes = gb_to_bytes(body.limit_gb) if body.limit_gb > 0 else None
    meta = await ensure_local(sid, kid)
    if not (meta and meta.get("disabled")):
        try:
            if limit_bytes is not None:
                await api.set_data_limit(kid, limit_bytes)
            else:
                await api.remove_data_limit(kid)
        except OutlineError as e:
            raise errors.upstream(str(e))
    await db.set_limit(sid, kid, limit_bytes)
    return {"ok": True, "limit": limit_bytes}


@router.put("/servers/{sid}/keys/{kid}/monthly")
async def set_key_monthly(sid: str, kid: str, body: MonthlyBody,
                          admin: dict = Depends(require("keys.edit"))):
    deny_free(admin)
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)
    if body.monthly_gb > 0:
        monthly = gb_to_bytes(body.monthly_gb)
        # Seed the first cycle's allowance on Outline (create_key_for:157 does
        # the same). Without it the quota is bookkeeping only and the key runs
        # unmetered until the first scheduler reset, a full cycle away.
        if meta.get("limit_bytes") is None and not meta.get("disabled"):
            try:
                usage = await api.get_transfer_metrics()
                # same shape as the scheduler's reset (scheduler.py:89-92):
                # limit_bytes holds the cumulative ceiling, not the plan size
                new_limit = int(usage.get(str(kid), 0)) + monthly
                await api.set_data_limit(kid, new_limit)
            except OutlineError as e:
                raise errors.upstream(str(e))
            await db.set_limit(sid, kid, new_limit)
        # First reset one cycle out (like create_key_for) so saving a quota
        # doesn't trigger an immediate reset on the next scheduler pass.
        await db.set_monthly(sid, kid, monthly,
                             int(time.time()) + await settings.cycle_seconds())
    else:
        await db.set_monthly(sid, kid, None, None)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/disable", dependencies=[Depends(require("keys.edit"))])
async def disable_key(sid: str, kid: str):
    api = api_or_404(sid)
    await ensure_local(sid, kid)
    try:
        await api.set_data_limit(kid, 0)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await db.set_disabled(sid, kid, True)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/enable", dependencies=[Depends(require("keys.edit"))])
async def enable_key(sid: str, kid: str):
    api = api_or_404(sid)
    meta = await db.get_key(sid, kid)
    try:
        await enable_on_outline(api, kid, meta or {})
    except OutlineError as e:
        raise errors.upstream(str(e))
    await db.set_disabled(sid, kid, False)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/extend")
async def extend_key(sid: str, kid: str, request: Request, body: ExtendBody,
                     admin: dict = Depends(require("keys.edit"))):
    """Adjust a key's validity: positive `days` extends (and re-enables a
    disabled key), negative `days` shortens it (clamped to expire-now).

    A credit admin renews by buying a package instead: any time or volume that
    reaches a user is paid for, so they cannot extend their way around the
    price list.
    """
    guard = await idempotency.begin(request, admin)
    if guard.replay:
        return guard.replay
    try:
        bought = await _buy(admin, body.package_id, sid, kid)
    except BaseException:
        await guard.abandon()
        raise
    if bought:
        try:
            result = await _apply_package(sid, kid, bought[0])
        except BaseException as e:
            await _reverse(admin, bought, sid, f"renew failed: {e}")
            await guard.abandon()
            raise
        await guard.done(result)
        return result
    if body.days == 0:
        await guard.abandon()
        raise HTTPException(status_code=400, detail="days must not be zero")
    # No money on this path, but the days still stack up on a double-send.
    try:
        api = api_or_404(sid)
        meta = await ensure_local(sid, kid)
        now = int(time.time())
        # Re-enable FIRST: committing the new expiry before the Outline call means a
        # 502 still moves the date, and the admin's retry extends a second time.
        # only re-enable on an extension, never on a reduction
        if body.days > 0 and meta.get("disabled"):
            try:
                await enable_on_outline(api, kid, meta)
            except OutlineError as e:
                raise errors.upstream(str(e))
        if meta.get("duration_days") is not None and meta.get("activated_ts") is None:
            # not yet activated — adjust the stored duration (min 1 day)
            await db.set_duration(sid, kid,
                                  max(1, int(meta["duration_days"]) + body.days))
        else:
            base = max(meta.get("expiry_ts") or 0, now)
            await db.set_expiry(sid, kid, max(now, base + body.days * 86400))
        if body.days > 0 and meta.get("disabled"):
            await db.set_disabled(sid, kid, False)
    except BaseException:
        await guard.abandon()
        raise
    result = {"ok": True}
    await guard.done(result)
    return result


@router.post("/servers/{sid}/keys/{kid}/reset")
async def reset_usage(sid: str, kid: str,
                      admin: dict = Depends(require("keys.edit"))):
    """Give the key a fresh allowance now (used + quota), and re-enable it.

    Outline's usage counter is cumulative and can't be zeroed, so a "reset"
    raises the data limit to current-usage + the per-cycle allowance.
    """
    deny_free(admin)
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)
    # Only monthly_bytes may be the base. limit_bytes is the *cumulative ceiling*
    # this endpoint itself writes below, so using it would compound every cycle
    # (10 -> 20 -> 40 GB). A plain data limit has no per-cycle size to restore.
    base = meta.get("monthly_bytes")
    if not base:
        raise HTTPException(status_code=400, detail="Set a monthly quota first")
    try:
        usage = await api.get_transfer_metrics()
        used = int(usage.get(str(kid), 0))
        new_limit = used + int(base)
        await api.set_data_limit(kid, new_limit)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await db.set_limit(sid, kid, new_limit)
    await db.set_disabled(sid, kid, False)
    return {"ok": True, "limit": new_limit}


async def _sub_info(token: str, admin: dict) -> dict:
    """Members of a subscription + which configured servers are included.

    The server list is what the UI offers as "mirror onto…", so it is filtered
    to the caller's scope — otherwise a sub-admin would see every server's name
    here even though everything else hides them.
    """
    members = await db.get_keys_by_sub_token(token)
    member_sids = {m["server_id"] for m in members}
    base = await settings.get_profile_base()
    return {
        "token": token,
        # The short customer link when a profile host is configured, the
        # long-standing /sub/ path otherwise — one field, so the UI shows
        # whatever this panel is actually able to serve.
        "profileUrl": f"{base}/{token}" if base else None,
        "path": f"/sub/{token}",
        "members": [
            {"serverId": m["server_id"],
             "serverName": (reg.meta(m["server_id"]) or {}).get("name"),
             "keyId": m["key_id"], "name": m.get("name")}
            for m in members if can_see(admin, m["server_id"])
        ],
        "servers": [
            {"id": s, "name": reg.meta(s)["name"], "included": s in member_sids}
            for s in scoped_ids(admin)
        ],
    }


@router.post("/servers/{sid}/keys/{kid}/sub")
async def make_sub_link(sid: str, kid: str,
                        admin: dict = Depends(require("keys.edit"))):
    """Ensure the key has a stable subscription token; return it + members."""
    api_or_404(sid)
    meta = await ensure_local(sid, kid)
    token = meta.get("sub_token")
    if not token:
        token = security.profile_token(kid)
        await db.set_sub_token(sid, kid, token)
    return await _sub_info(token, admin)


@router.post("/servers/{sid}/keys/{kid}/rotate")
async def rotate_key(sid: str, kid: str,
                     admin: dict = Depends(require("keys.edit"))):
    """Give this customer a fresh Outline key, keeping everything else.

    For when a config stops working — the address gets blocked, or the key
    leaks. The profile URL does not change, so the customer re-opens the link
    they already have and finds the new config waiting.

    **Outline counts usage per key.** A new key starts at zero, so replacing one
    naively hands the customer their whole allowance back — the same shape of
    hole as the free-renewal paths closed earlier, except this one refunds data
    instead of money. The remaining allowance is carried across instead: the new
    key's ceiling is the old ceiling minus what was already spent.

    Order matters. The new key is created *before* the old one is deleted, so a
    failure half way leaves the customer with a working config and at worst a
    stale key to clean up — rather than no config at all.
    """
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)

    try:
        usage = await api.get_transfer_metrics()
        used = int(usage.get(str(kid), 0))
    except OutlineError as e:
        raise errors.upstream(str(e))

    old_limit = meta.get("limit_bytes")
    if old_limit is None:
        new_limit = None                      # unlimited stays unlimited
    else:
        # Never below zero: a key already over its ceiling rotates to 0, which
        # Outline reads as "blocked" — the same state it was in.
        new_limit = max(0, int(old_limit) - used)

    name = meta.get("name") or f"Key {kid}"
    try:
        fresh = await api.create_key(name=name, limit_bytes=new_limit)
    except OutlineError as e:
        raise errors.upstream(str(e))
    new_kid = fresh["id"]

    try:
        await db.add_key(sid, new_kid, name, new_limit,
                         meta.get("duration_days"),
                         owner_admin_id=meta.get("owner_admin_id"))
        # Carry the clock exactly: a rotation is not a renewal, and must not
        # restart a validity period the customer has already been running down.
        if meta.get("activated_ts"):
            await db.activate(sid, new_kid, int(meta["activated_ts"]),
                              int(meta.get("expiry_ts") or 0))
        if meta.get("monthly_bytes"):
            await db.set_monthly(sid, new_kid, int(meta["monthly_bytes"]),
                                 meta.get("reset_ts"))
        if meta.get("disabled"):
            await api.set_data_limit(new_kid, 0)
            await db.set_disabled(sid, new_kid, True)
        if meta.get("sub_token"):
            await db.set_sub_token(sid, new_kid, meta["sub_token"])
    except Exception as e:  # noqa: BLE001 — don't strand a half-built key
        log.exception("rotate: persist failed, removing the new key")
        try:
            await api.delete_key(new_kid)
        except OutlineError:
            pass
        await db.delete_key(sid, new_kid)
        raise HTTPException(status_code=500, detail=f"Could not rotate: {e}")

    # Only now is the old one safe to drop.
    try:
        await api.delete_key(kid)
    except OutlineError as e:
        if e.status != 404:
            # The customer already has a working config; say so rather than
            # rolling back, and leave the stale key for the admin to remove.
            log.warning("rotate: new key %s is live but the old one (%s) "
                        "could not be deleted: %s", new_kid, kid, e)
    await db.delete_key(sid, kid)
    sub_router.invalidate(meta.get("sub_token"))

    metrics.inc("outline_panel_keys_rotated_total", {"server": sid})
    return {"ok": True, "id": new_kid, "previousId": kid,
            "accessUrl": fresh.get("accessUrl"),
            "limit": new_limit, "carriedUsed": used,
            "subToken": meta.get("sub_token")}


@router.post("/sub/{token}/servers/{target}")
async def sub_add_server(token: str, target: str,
                         admin: dict = Depends(require("keys.edit"))):
    """Mirror the subscription onto `target`: create a key there (cloning the
    primary's name/limit/duration) and join it to the same token."""
    # `target`, not `sid`, so enforce_scope never sees it: check it by hand or
    # this route mints a key on any server in the panel.
    if not can_see(admin, target):
        raise errors.unknown_server()
    # A mirror is a whole second key with the primary's allowance — product, and
    # it was free. A credit admin buys a package per server instead.
    deny_free(admin)
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise errors.unknown_subscription()
    if any(m["server_id"] == target for m in members):
        return await _sub_info(token, admin)  # already included
    api = api_or_404(target)
    primary = members[0]
    name = primary.get("name") or "user"
    limit_bytes = primary.get("limit_bytes")
    duration = primary.get("duration_days")
    try:
        key = await api.create_key(name=name, limit_bytes=limit_bytes)
        # The mirror is the same subscription, so it inherits the primary's
        # state — not a fresh one. Without this an expired, suspended user gets
        # a live config with a full allowance and a clock that restarts.
        if primary.get("disabled"):
            await api.set_data_limit(key["id"], 0)
    except OutlineError as e:
        raise errors.upstream(str(e))
    try:
        await db.add_key(target, key["id"], name, limit_bytes, duration,
                         owner_admin_id=primary.get("owner_admin_id"))
        if primary.get("activated_ts"):
            await db.activate(target, key["id"], int(primary["activated_ts"]),
                              int(primary["expiry_ts"] or 0))
        if primary.get("disabled"):
            await db.set_disabled(target, key["id"], True)
        await db.set_sub_token(target, key["id"], token)
    except Exception as e:  # noqa: BLE001 — don't leave an orphan key
        log.exception("sub mirror persist failed; deleting orphan key")
        try:
            await api.delete_key(key["id"])
        except OutlineError:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to add server: {e}")
    sub_router.invalidate(token)
    return await _sub_info(token, admin)


@router.delete("/sub/{token}/servers/{target}")
async def sub_remove_server(token: str, target: str,
                            admin: dict = Depends(require("keys.edit"))):
    """Remove `target`'s config from the subscription (unlinks the token; the
    key itself is kept — delete it from the key list if no longer needed)."""
    if not can_see(admin, target):  # `target`, so enforce_scope misses it too
        raise errors.unknown_server()
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise errors.unknown_subscription()
    for m in members:
        if m["server_id"] == target:
            await db.set_sub_token(target, m["key_id"], None)
    sub_router.invalidate(token)   # the removed config must stop being served
    return await _sub_info(token, admin)


class OwnerBody(BaseModel):
    # null hands the key back to the panel owner
    admin_id: int | None = None


@router.put("/servers/{sid}/keys/{kid}/owner", dependencies=[Depends(require_owner)])
async def set_key_owner(sid: str, kid: str, body: OwnerBody):
    """Move a user onto another admin's page.

    Owner-only: ownership decides who may see and bill a customer, so letting a
    reseller reassign one would let them hand it off — or put it out of reach.
    """
    await ensure_local(sid, kid)
    target = None
    if body.admin_id is not None:
        target = await db.get_admin(body.admin_id)
        if target is None:
            raise errors.unknown_admin()
    if target is None or target["is_owner"]:
        await db.set_key_owner(sid, kid, None)   # the owner is stored as NULL
        return {"ok": True, "ownerAdminId": None}
    # "only if I gave them access": handing a user to an admin who cannot reach
    # the server would strand it — invisible to them, and no longer on your page.
    if not can_see(target, sid):
        raise HTTPException(
            status_code=400,
            detail=f"{target['username']} does not have access to this server",
        )
    await db.set_key_owner(sid, kid, target["id"])
    return {"ok": True, "ownerAdminId": target["id"]}


@router.delete("/servers/{sid}/keys/{kid}", dependencies=[Depends(require("keys.delete"))])
async def delete_key(sid: str, kid: str):
    api = api_or_404(sid)
    try:
        await api.delete_key(kid)
    except OutlineError as e:
        # Already gone upstream (deleted straight from Outline Manager) is a
        # success for us: still drop the local row, or it becomes an
        # undeletable ghost — invisible in the key list, yet still holding the
        # subscription token that sub_add_server clones from.
        if e.status != 404:
            raise errors.upstream(str(e))
    meta = await db.get_key(sid, kid)
    await db.delete_key(sid, kid)
    # the config is gone; stop serving it from the cached subscription too
    sub_router.invalidate((meta or {}).get("sub_token"))
    metrics.inc("outline_panel_keys_deleted_total", {"server": sid})
    return {"ok": True}
