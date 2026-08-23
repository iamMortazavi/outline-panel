"""Access-key (user) management across all servers."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...application import customer
from ...application.executor import Executor
from ...core import errors, metrics, security
from ...core.outline_api import OutlineAPI, OutlineError
from ...core.utils import gb_to_bytes
from .. import idempotency
from ..deps import (
    api_or_404,
    assert_key_access,
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
from ..schemas import BulkResult, KeyCreated, KeyList, LimitOk, Ok, OwnerSet, Rotated, SubInfoAdmin
from . import subscription as sub_router

log = logging.getLogger("web.keys")

# One customer, every server they are on. Endpoints still name a single key
# because that is what a URL and an Outline server understand; nothing past
# this point acts on one member (see MODERNIZATION.md A1).
ops = Executor(reg, db)


def _ok(deferred: list[dict]) -> dict:
    """The response every write shares.

    Plain `{"ok": True}` when every member reached its server, which is the
    shape the panel has always returned. `pending` appears only when some
    member did not: the panel has recorded the intent and a worker is
    retrying, and saying nothing would be claiming an effect that has not
    happened yet.
    """
    return {"ok": True, "pending": deferred} if deferred else {"ok": True}
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


@router.get("/keys", response_model=KeyList, response_model_exclude_unset=True)
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

    Adoption asks Outline whether the key is real first. It used to take the id
    on faith, so a key id that never existed got a row, a customer link and —
    once anything mirrored it onto a second server — an actual Outline key built
    from the invented row. Only this branch pays the lookup: an id we have a row
    for is one we have already seen.
    """
    meta = await db.get_key(sid, kid)
    if not meta:
        try:
            await api_or_404(sid).get_key(kid)
        except OutlineError:
            raise errors.unknown_key()
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
    # Extra servers to put this customer on, beyond the one in the path. One
    # subscription, one link, a config on each — see mirror_onto for why the
    # allowance is not divided.
    extra_servers: list[str] = []


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
    quota and resetting usage all reach a user just as surely, and all three
    were free: a reseller bought the cheapest package and then topped it up here
    for nothing.

    Mirroring onto a second server is the deliberate exception — see
    `mirror_onto`. It is failover for a customer already paid for, not more
    product, so it does not call this.
    """
    if on_credit(admin):
        raise errors.buy_a_package()


async def _apply_package(sid: str, kid: str, pkg: dict) -> dict:
    """Add a package's time and volume to an existing customer (a renewal).

    Every server they are on, not just the one in the URL: a renewal that moves
    one row leaves the scheduler to cut the customer off everywhere else at the
    old date, after they paid.

    `limit_bytes` is the *cumulative ceiling* Outline counts against, never a
    plan size, so adding to it is the correct operation (invariant T2).
    """
    api_or_404(sid)
    await ensure_local(sid, kid)
    deferred, new_limit = await customer.apply_package(db, ops, sid, kid, pkg)
    return {**_ok(deferred), "limit": new_limit}


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


async def _add_extra_servers(key: dict, extras: list[str], sid: str,
                             admin: dict) -> dict:
    """Mirror a freshly created key onto the other servers that were picked.

    Failures here do **not** undo the creation. The customer already has a
    working config and, for a credit admin, the package is already paid for —
    unwinding that because a third server was briefly unreachable would be worse
    than handing back a key that works on two. The ones that failed are named in
    the response so the panel can say so.
    """
    wanted = [t for t in dict.fromkeys(extras) if t and t != sid]
    if not wanted or not key.get("subToken"):
        return key
    added, failed = [], []
    for target in wanted:
        if not can_see(admin, target) or reg.meta(target) is None:
            failed.append({"id": target, "error": "Unknown server"})
            continue
        try:
            await mirror_onto(key["subToken"], target)
            added.append(target)
        except HTTPException as e:
            failed.append({"id": target, "error": str(e.detail)})
        except Exception as e:  # noqa: BLE001
            log.exception("could not mirror new key onto %s", target)
            failed.append({"id": target, "error": str(e)})
    key["servers"] = [sid, *added]
    if failed:
        key["serverErrors"] = failed
    return key


@router.post("/servers/{sid}/keys", response_model=KeyCreated, response_model_exclude_unset=True)
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
            key = await _add_extra_servers(key, body.extra_servers, sid, admin)
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
    key = await _add_extra_servers(key, body.extra_servers, sid, admin)
    await guard.done(key)
    return key


@router.put("/servers/{sid}/keys/{kid}/name", response_model=Ok, response_model_exclude_unset=True,
            dependencies=[Depends(require("keys.edit"))])
async def rename_key(sid: str, kid: str, body: NameBody):
    api = api_or_404(sid)
    try:
        await api.rename_key(kid, body.name)
    except OutlineError as e:
        raise errors.upstream(str(e))
    await ensure_local(sid, kid)
    await db.set_name(sid, kid, body.name)
    return {"ok": True}


@router.put("/servers/{sid}/keys/{kid}/limit", response_model=LimitOk,
            response_model_exclude_unset=True)
async def set_key_limit(sid: str, kid: str, body: LimitBody,
                        admin: dict = Depends(require("keys.edit"))):
    deny_free(admin)
    api_or_404(sid)
    limit_bytes = gb_to_bytes(body.limit_gb) if body.limit_gb > 0 else None
    await ensure_local(sid, kid)
    deferred = await customer.set_allowance(db, ops, sid, kid, limit_bytes)
    return {**_ok(deferred), "limit": limit_bytes}


@router.put("/servers/{sid}/keys/{kid}/monthly", response_model=Ok,
            response_model_exclude_unset=True)
async def set_key_monthly(sid: str, kid: str, body: MonthlyBody,
                          admin: dict = Depends(require("keys.edit"))):
    deny_free(admin)
    api_or_404(sid)
    await ensure_local(sid, kid)
    monthly = gb_to_bytes(body.monthly_gb) if body.monthly_gb > 0 else None
    # The first reset is one cycle out, so saving a quota does not trigger an
    # immediate reset on the next scheduler pass. Seeding this cycle's allowance
    # upstream is the aggregate's job — without it the quota is bookkeeping only
    # and the key runs unmetered until that first reset, a whole cycle away.
    deferred = await customer.set_monthly(db, ops, reg, sid, kid, monthly,
                                          await settings.cycle_seconds())
    return _ok(deferred)


@router.post("/servers/{sid}/keys/{kid}/disable", response_model=Ok, response_model_exclude_unset=True,
             dependencies=[Depends(require("keys.edit"))])
async def disable_key(sid: str, kid: str):
    """Suspend the customer — on every server they are on.

    Suspending only the key named in the URL left them fully live on the mirror,
    which is the server their client fails over to the moment the primary stops
    answering. That is not a suspension.
    """
    api_or_404(sid)
    await ensure_local(sid, kid)
    return _ok(await customer.suspend(db, ops, sid, kid))


@router.post("/servers/{sid}/keys/{kid}/enable", response_model=Ok, response_model_exclude_unset=True,
             dependencies=[Depends(require("keys.edit"))])
async def enable_key(sid: str, kid: str):
    """The mirror image of suspend: paying again brings back every server."""
    api_or_404(sid)
    return _ok(await customer.resume(db, ops, sid, kid))


@router.post("/servers/{sid}/keys/{kid}/extend", response_model=LimitOk, response_model_exclude_unset=True)
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
        api_or_404(sid)
        await ensure_local(sid, kid)
        deferred = await customer.extend(db, ops, sid, kid, body.days)
    except BaseException:
        await guard.abandon()
        raise
    result = _ok(deferred)
    await guard.done(result)
    return result


@router.post("/servers/{sid}/keys/{kid}/reset", response_model=LimitOk,
             response_model_exclude_unset=True)
async def reset_usage(sid: str, kid: str,
                      admin: dict = Depends(require("keys.edit"))):
    """Give the key a fresh allowance now (used + quota), and re-enable it.

    Outline's usage counter is cumulative and can't be zeroed, so a "reset"
    raises the data limit to current-usage + the per-cycle allowance.
    """
    deny_free(admin)
    api_or_404(sid)
    meta = await ensure_local(sid, kid)
    # Only monthly_bytes may be the base. limit_bytes is the *cumulative ceiling*
    # this endpoint itself writes, so using it would compound every cycle
    # (10 -> 20 -> 40 GB). A plain data limit has no per-cycle size to restore.
    if not meta.get("monthly_bytes"):
        raise HTTPException(status_code=400, detail="Set a monthly quota first")
    # Recomputed per member: Outline counts usage per key, so "used + allowance"
    # is a different number on each server the customer is on.
    deferred, new_limit = await customer.reset_usage(db, ops, reg, sid, kid)
    return {**_ok(deferred), "limit": new_limit}


class KeyRef(BaseModel):
    server_id: str
    key_id: str


class BulkServerBody(BaseModel):
    keys: list[KeyRef] = Field(min_length=1, max_length=500)
    # "add" puts every selected customer on this server; "remove" takes the
    # server's config back out of their subscription.
    action: str = Field(pattern="^(add|remove)$")


@router.post("/servers/{sid}/bulk-servers", response_model=BulkResult, response_model_exclude_unset=True)
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
            meta = await ensure_local(ref.server_id, ref.key_id)
            token = meta.get("sub_token")
            if not token:
                # An older key with no subscription yet. Mint one rather than
                # skipping it — otherwise the oldest customers are exactly the
                # ones this feature cannot help.
                token = security.profile_token(ref.key_id)
                await db.set_sub_token(ref.server_id, ref.key_id, token)
            await sub_or_404(token, admin)
            if body.action == "add":
                await mirror_onto(token, sid)
            else:
                await _unmirror(token, sid)
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


async def _unmirror(token: str, target: str) -> None:
    """Drop `target`'s config out of a subscription. Caller checks access."""
    for m in await db.get_keys_by_sub_token(token):
        if m["server_id"] == target:
            await db.set_sub_token(target, m["key_id"], None)
    sub_router.invalidate(token)   # the removed config must stop being served


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


@router.post("/servers/{sid}/keys/{kid}/sub", response_model=SubInfoAdmin, response_model_exclude_unset=True)
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


@router.post("/servers/{sid}/keys/{kid}/rotate", response_model=Rotated, response_model_exclude_unset=True)
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


async def mirror_onto(token: str, target: str) -> None:
    """Put this subscription on `target` too, cloning the primary.

    The customer gets the same allowance on every server they are given. That
    is a deliberate business decision by the panel owner, not an oversight: a
    multi-server subscription is sold for failover, people realistically use one
    server at a time, and metering it any other way either cuts someone off
    mid-month or doubles what they pay for a fallback they rarely touch. It does
    mean a determined customer could spend the full allowance on each server —
    the owner absorbs that.

    Caller checks scope. Raises on failure; nothing is left half-built.
    """
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise errors.unknown_subscription()
    if any(m["server_id"] == target for m in members):
        return                                   # already included
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
        # The monthly quota is part of the plan, not of the primary's key. Left
        # off, the mirror was invisible to the scheduler's reset pass and to
        # `reset_usage`, so it kept the ceiling it was born with for good —
        # while the customer's own page showed a quota that refreshed.
        if primary.get("monthly_bytes"):
            await db.set_monthly(target, key["id"], int(primary["monthly_bytes"]),
                                 primary.get("reset_ts"))
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


async def sub_or_404(token: str, admin: dict) -> list[dict]:
    """This subscription's keys — or 404, because it is not the caller's.

    These two routes are keyed by `token`, not by `{sid}/{kid}`, so
    `enforce_scope` never runs on them. Checking `target` alone only answered
    "may you use that server": the *subscription* went unchecked, and the token
    is the customer link — it is forwarded, pasted into groups, and every
    reseller holds the ones they sold. Any admin with keys.edit could therefore
    mint a config on a rival's customer, or unlink one and cut them off.

    A subscription must be wholly the caller's: one member on a server they
    cannot see is still someone else's customer.
    """
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise errors.unknown_subscription()
    if not is_owner(admin) and not all(
            can_see(admin, m["server_id"]) and owns(admin, m) for m in members):
        raise errors.unknown_subscription()
    return members


@router.post("/sub/{token}/servers/{target}", response_model=SubInfoAdmin, response_model_exclude_unset=True)
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
    await sub_or_404(token, admin)
    await mirror_onto(token, target)
    return await _sub_info(token, admin)


@router.delete("/sub/{token}/servers/{target}", response_model=SubInfoAdmin, response_model_exclude_unset=True)
async def sub_remove_server(token: str, target: str,
                            admin: dict = Depends(require("keys.edit"))):
    """Remove `target`'s config from the subscription (unlinks the token; the
    key itself is kept — delete it from the key list if no longer needed)."""
    if not can_see(admin, target):  # `target`, so enforce_scope misses it too
        raise errors.unknown_server()
    await sub_or_404(token, admin)
    await _unmirror(token, target)
    return await _sub_info(token, admin)


class OwnerBody(BaseModel):
    # null hands the key back to the panel owner
    admin_id: int | None = None


@router.put("/servers/{sid}/keys/{kid}/owner", response_model=OwnerSet, response_model_exclude_unset=True,
            dependencies=[Depends(require_owner)])
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


@router.delete("/servers/{sid}/keys/{kid}", response_model=Ok, response_model_exclude_unset=True,
               dependencies=[Depends(require("keys.delete"))])
async def delete_key(sid: str, kid: str):
    """Delete the customer — every server they are on.

    Deleting only the named key left live configs on every mirror *and* left the
    public subscription serving them, with no panel row to find them by. An
    upstream 404 is still success: a key deleted straight from Outline Manager
    must not become an undeletable ghost here.
    """
    api_or_404(sid)
    meta = await db.get_key(sid, kid)
    gone = len((await customer.load(db, sid, kid)).members) or 1
    deferred = await customer.remove(db, ops, sid, kid)
    # the configs are gone; stop serving them from the cached subscription too
    sub_router.invalidate((meta or {}).get("sub_token"))
    metrics.inc("outline_panel_keys_deleted_total", {"server": sid}, by=float(gone))
    return _ok(deferred)
