"""Access-key (user) management across all servers.

The HTTP layer only: what a route is called, who may call it, and the shape of
its answer. What creating, charging for, mirroring or adopting a key *means*
lives in ``services.keys`` — the Telegram bot and the Mini App need exactly
that, and a route function is not something they should be reaching into.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core import errors, metrics, security
from ...core.concurrency import map_concurrently
from ...core.outline_api import OutlineAPI, OutlineError
from ...core.settings import SettingsView
from ...core.utils import gb_to_bytes
from .. import idempotency, subcache
from ..deps import (
    api_or_404,
    assert_key_access,
    can_see,
    db,
    enforce_scope,
    owns,
    reg,
    require,
    require_owner,
    settings,
    sids_or_404,
)
from ..services import keys as ksvc

# The purchase shapes live beside the logic that charges for them; re-exported
# here because FastAPI reads them off this module and the Mini App imports them.
from ..services.keys import CreateBody, ExtendBody

__all__ = ["router", "CreateBody", "ExtendBody"]

log = logging.getLogger("web.keys")
router = APIRouter(prefix="/api", tags=["keys"],
                   dependencies=[Depends(enforce_scope)])

# --------------------------------------------------------------- read helpers
async def _conn_info(api: OutlineAPI, ttl: int) -> dict[str, dict]:
    try:
        m = await api.get_server_metrics_cached("30d", ttl)
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


async def keys_for_server(sid: str, admin: dict, names: dict,
                          cfg: SettingsView) -> dict:
    m = reg.meta(sid)
    if m is None:  # server removed between snapshot and fetch
        return {"serverId": sid, "serverName": None, "keys": [], "error": "Server removed"}
    api = m["api"]
    try:
        # Run the three upstream reads concurrently on the reused connection
        # pool; _conn_info swallows its own errors, so only list_keys /
        # get_transfer_metrics raising OutlineError lands in the except below.
        keys, usage, conn = await asyncio.gather(
            api.list_keys(), api.get_transfer_metrics(),
            _conn_info(api, cfg.num("metrics_ttl")),
        )
    except OutlineError as e:
        # Server briefly unreachable — surface the error, don't drop its keys.
        return {"serverId": sid, "serverName": m["name"], "keys": [], "error": str(e)}
    local = {k["key_id"]: k for k in await db.keys_for(sid)}
    base = cfg.profile_base()
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
    # Read the settings once for the whole request. Each server used to re-read
    # the metrics TTL and the profile base for itself, so listing keys cost two
    # extra row reads per configured server on every poll.
    cfg = await settings.view()
    results = await map_concurrently(sids, lambda s: keys_for_server(s, admin, names, cfg))
    keys = [k for r in results for k in r["keys"]]
    keys.sort(key=lambda x: (x["serverName"] or "", int(x["id"]) if str(x["id"]).isdigit() else 0))
    errors = [
        {"serverId": r["serverId"], "serverName": r["serverName"], "error": r["error"]}
        for r in results if r["error"]
    ]
    return {"keys": keys, "errors": errors}

# --------------------------------------------------------------------- models

class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LimitBody(BaseModel):
    limit_gb: float = Field(ge=0)


class MonthlyBody(BaseModel):
    monthly_gb: float = Field(ge=0)


# --------------------------------------------------------------------- routes

@router.post("/servers/{sid}/keys")
async def create_key(sid: str, request: Request, body: CreateBody,
                     admin: dict = Depends(require("keys.create"))):
    """Create — or sell — a key on this server.

    Every door into this (dashboard, Mini App, Telegram) lands on the same
    service call, so a reseller cannot find a cheaper one by changing client.
    """
    return await ksvc.create_key_flow(admin, sid, body, request)



@router.put("/servers/{sid}/keys/{kid}/name", dependencies=[Depends(require("keys.edit"))])
async def rename_key(sid: str, kid: str, body: NameBody):
    api = api_or_404(sid)
    # Adopt — and so validate — before touching Outline, the way every other
    # edit route does. Renaming first meant an id that exists nowhere came back
    # as 502 "the upstream failed" instead of 404 "there is no such key", and
    # a key the panel had never seen was renamed upstream before anything
    # checked whether it was real.
    await ksvc.ensure_local(sid, kid)
    try:
        await api.rename_key(kid, body.name)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await db.set_name(sid, kid, body.name)
    return {"ok": True}


@router.put("/servers/{sid}/keys/{kid}/limit")
async def set_key_limit(sid: str, kid: str, body: LimitBody,
                        admin: dict = Depends(require("keys.edit"))):
    ksvc.deny_free(admin)
    api = api_or_404(sid)
    limit_bytes = gb_to_bytes(body.limit_gb) if body.limit_gb > 0 else None
    meta = await ksvc.ensure_local(sid, kid)
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
    ksvc.deny_free(admin)
    api = api_or_404(sid)
    meta = await ksvc.ensure_local(sid, kid)
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
    await ksvc.ensure_local(sid, kid)
    try:
        await api.set_data_limit(kid, 0)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await db.set_disabled(sid, kid, True)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/enable", dependencies=[Depends(require("keys.edit"))])
async def enable_key(sid: str, kid: str):
    api = api_or_404(sid)
    # ensure_local rather than get_key: `disable` on the same key already
    # adopts it, so enabling was the one side of the pair that left a key
    # unadopted — and answered 502 for an id that does not exist, where its
    # twin answers 404.
    meta = await ksvc.ensure_local(sid, kid)
    try:
        await ksvc.enable_on_outline(api, kid, meta)
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
        bought = await ksvc.buy_package(admin, body.package_id, sid, kid)
    except BaseException:
        await guard.abandon()
        raise
    if bought:
        try:
            result = await ksvc.apply_package(sid, kid, bought[0])
        except BaseException as e:
            await ksvc.refund(admin, bought, sid, f"renew failed: {e}")
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
        meta = await ksvc.ensure_local(sid, kid)
        now = int(time.time())
        # Re-enable FIRST: committing the new expiry before the Outline call means a
        # 502 still moves the date, and the admin's retry extends a second time.
        # only re-enable on an extension, never on a reduction
        if body.days > 0 and meta.get("disabled"):
            try:
                await ksvc.enable_on_outline(api, kid, meta)
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
    ksvc.deny_free(admin)
    api = api_or_404(sid)
    meta = await ksvc.ensure_local(sid, kid)
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


class KeyRef(BaseModel):
    server_id: str
    key_id: str


class BulkServerBody(BaseModel):
    keys: list[KeyRef] = Field(min_length=1, max_length=500)
    # "add" puts every selected customer on this server; "remove" takes the
    # server's config back out of their subscription.
    action: str = Field(pattern="^(add|remove)$")


@router.post("/servers/{sid}/bulk-servers")
async def bulk_server_membership(sid: str, body: BulkServerBody,
                                 admin: dict = Depends(require("keys.edit"))):
    """Put a whole selection of customers on this server, or take them off it.

    This existed only as one-customer-at-a-time in the subscription sheet, which
    meant that moving a group onto a new server was a database script — the panel
    could not do the single most ordinary thing an operator does after renting a
    server. `{sid}` (not `{target}`) so enforce_scope checks the destination for
    us; every key is then checked individually, because a selection is just a
    list of ids the browser sent and none of it is trustworthy.

    Partial success is the normal outcome, not an error: one unreachable server
    or one key someone else owns must not sink the other fourteen. Every item
    comes back with its own verdict and the caller reports them.
    """
    api_or_404(sid)
    done: list[dict] = []
    failed: list[dict] = []
    for ref in body.keys:
        label = f"{ref.server_id}/{ref.key_id}"
        try:
            # The same gate the single-key routes get from enforce_scope: scope
            # on the key's *own* server, plus ownership of the key itself.
            await assert_key_access(admin, ref.server_id, ref.key_id)
            meta = await ksvc.ensure_local(ref.server_id, ref.key_id)
            token = meta.get("sub_token")
            if not token:
                # An older key with no subscription yet. Mint one rather than
                # skipping it — otherwise the oldest customers are exactly the
                # ones this feature cannot help.
                token = security.profile_token(ref.key_id)
                await db.set_sub_token(ref.server_id, ref.key_id, token)
            await ksvc.sub_or_404(token, admin)
            if body.action == "add":
                await ksvc.mirror_onto(token, sid)
            else:
                await ksvc.unmirror(token, sid)
            done.append({"serverId": ref.server_id, "keyId": ref.key_id,
                         "name": meta.get("name") or ref.key_id, "token": token})
        except HTTPException as e:
            failed.append({"key": label, "error": str(e.detail)})
        except Exception as e:  # noqa: BLE001 — one bad key must not sink the batch
            log.exception("bulk %s failed for %s", body.action, label)
            failed.append({"key": label, "error": str(e)})
    metrics.inc("outline_panel_bulk_server_total", {"server": sid,
                                                    "action": body.action})
    return {"ok": True, "action": body.action, "server": sid,
            "done": done, "failed": failed}

@router.post("/servers/{sid}/keys/{kid}/sub")
async def make_sub_link(sid: str, kid: str,
                        admin: dict = Depends(require("keys.edit"))):
    """Ensure the key has a stable subscription token; return it + members."""
    api_or_404(sid)
    meta = await ksvc.ensure_local(sid, kid)
    token = meta.get("sub_token")
    if not token:
        token = security.profile_token(kid)
        await db.set_sub_token(sid, kid, token)
    return await ksvc.sub_info(token, admin)


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
    meta = await ksvc.ensure_local(sid, kid)

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
    subcache.invalidate(meta.get("sub_token"))

    metrics.inc("outline_panel_keys_rotated_total", {"server": sid})
    return {"ok": True, "id": new_kid, "previousId": kid,
            "accessUrl": fresh.get("accessUrl"),
            "limit": new_limit, "carriedUsed": used,
            "subToken": meta.get("sub_token")}


@router.post("/sub/{token}/servers/{target}")
async def sub_add_server(token: str, target: str,
                         admin: dict = Depends(require("keys.edit"))):
    """Add `target` to an existing customer's subscription."""
    # `target`, not `sid`, so enforce_scope never sees it: check it by hand or
    # this route mints a key on any server in the panel.
    if not can_see(admin, target):
        raise errors.unknown_server()
    # No deny_free here on purpose: mirroring is the one top-up the owner gives
    # away — see mirror_onto, and test_a_credit_admin_may_put_a_customer_on_
    # several_servers.
    await ksvc.sub_or_404(token, admin)
    await ksvc.mirror_onto(token, target)
    return await ksvc.sub_info(token, admin)


@router.delete("/sub/{token}/servers/{target}")
async def sub_remove_server(token: str, target: str,
                            admin: dict = Depends(require("keys.edit"))):
    """Remove `target`'s config from the subscription (unlinks the token; the
    key itself is kept — delete it from the key list if no longer needed)."""
    if not can_see(admin, target):  # `target`, so enforce_scope misses it too
        raise errors.unknown_server()
    await ksvc.sub_or_404(token, admin)
    await ksvc.unmirror(token, target)
    return await ksvc.sub_info(token, admin)


class OwnerBody(BaseModel):
    # null hands the key back to the panel owner
    admin_id: int | None = None


@router.put("/servers/{sid}/keys/{kid}/owner", dependencies=[Depends(require_owner)])
async def set_key_owner(sid: str, kid: str, body: OwnerBody):
    """Move a user onto another admin's page.

    Owner-only: ownership decides who may see and bill a customer, so letting a
    reseller reassign one would let them hand it off — or put it out of reach.
    """
    await ksvc.ensure_local(sid, kid)
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
    subcache.invalidate((meta or {}).get("sub_token"))
    metrics.inc("outline_panel_keys_deleted_total", {"server": sid})
    return {"ok": True}
