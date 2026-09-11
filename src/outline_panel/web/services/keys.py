"""What creating, selling and mirroring a key actually means.

Everything here was in ``routers/keys.py``, reachable only by calling a FastAPI
route function directly. The Telegram bot needs exactly this — the same charge,
the same reversal, the same ownership — so ``deps`` imported the router from
inside a function body to get at it, and the package had a cycle.

Now the logic sits below the HTTP layer and above the shared objects: routers
are thin wrappers over these, the bot is handed ``create_key_flow`` at startup,
and there is one implementation of "a key was sold" rather than a dashboard one
and a Telegram one that can drift apart.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ...core import errors, metrics, security
from ...core.outline_api import OutlineAPI, OutlineError
from ...core.utils import gb_to_bytes
from .. import idempotency, subcache
from ..state import (
    api_or_404,
    can_see,
    db,
    is_owner,
    on_credit,
    owns,
    price_for,
    reg,
    scoped_ids,
    settings,
)

log = logging.getLogger("web.keys")


# --------------------------------------------------------------------- models
class CreateBody(BaseModel):
    """What a caller asks for when buying or minting a key.

    Lives here rather than in the router because the bot builds one too: the
    field limits below are the only validation a Telegram button press gets.
    """

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


class ExtendBody(BaseModel):
    # positive extends validity (and re-enables); negative shortens it.
    days: int = Field(default=0, ge=-3650, le=3650)
    # A credit admin renews by buying another package instead of naming days.
    package_id: int | None = None


# --------------------------------------------------------------- local record
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


# --------------------------------------------------------------------- credit
async def buy_package(admin: dict, package_id: int | None, sid: str,
                      kid: str | None = None) -> tuple[dict, int, int] | None:
    """Charge `admin` for a package, or raise. Returns (package, price, entry id).

    Returns None when the caller is not on credit, meaning "no purchase, use
    the free-form path". Reserving BEFORE the Outline call is deliberate: the
    caller must reverse it if the call fails (see `refund`), because a check
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

    `extend_key` already enforces this through `buy_package` — "any time or
    volume that reaches a user is paid for". Raising the data limit, granting a
    monthly quota and resetting usage all reach a user just as surely, and all
    three were free: a reseller bought the cheapest package and then topped it
    up here for nothing.

    Mirroring onto a second server is the deliberate exception — see
    `mirror_onto`. It is failover for a customer already paid for, not more
    product, so it does not call this.
    """
    if on_credit(admin):
        raise errors.buy_a_package()


async def refund(admin: dict, bought: tuple[dict, int, int] | None, sid: str,
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


async def apply_package(sid: str, kid: str, pkg: dict) -> dict:
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


# -------------------------------------------------------------------- create
async def create_key_for(sid: str, name: str, limit_gb: float, days: int,
                         monthly_gb: float = 0, start_now: bool = False,
                         owner_admin_id: int | None = None) -> dict:
    """Create a key on Outline and persist its local metadata.

    Raises ``HTTPException`` on failure and removes any orphan key left on the
    server.

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
            await db.set_monthly(sid, key["id"], monthly_bytes,
                                 now + await settings.cycle_seconds())
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


async def add_extra_servers(key: dict, extras: list[str], sid: str,
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


async def create_key_flow(admin: dict, sid: str, body: CreateBody,
                          request: Request | None = None) -> dict:
    """Sell (or mint) one key, charge for it, and put it on any extra servers.

    The single definition of what creating a key costs and does. The dashboard
    route, the Mini App and the Telegram bot all land here, so a reseller cannot
    find a cheaper door by using a different client.
    """
    # The charge lands before Outline is called, so a lost response used to
    # leave the admin paying for a retry. An Idempotency-Key replays the first
    # answer instead.
    guard = await idempotency.begin(request, admin)
    if guard.replay:
        return guard.replay
    mine = None if is_owner(admin) else admin["id"]
    try:
        bought = await buy_package(admin, body.package_id, sid)
        if not bought:
            key = await create_key_for(sid, body.name, body.limit_gb, body.days,
                                       body.monthly_gb, body.start_now, mine)
            key = await add_extra_servers(key, body.extra_servers, sid, admin)
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
            await refund(admin, bought, sid, f"create failed: {e}")
            raise
    except BaseException:
        # Every failure above either charged nothing or reversed it, so the key
        # is free to be retried.
        await guard.abandon()
        raise
    # now that the key exists, point the charge at what it bought
    await db.tag_ledger(entry, sid, key["id"])
    key = await add_extra_servers(key, body.extra_servers, sid, admin)
    await guard.done(key)
    return key


async def create_key_as(admin: dict, sid: str, **fields) -> dict:
    """Create a key exactly the way the dashboard does, on behalf of `admin`.

    The Telegram bot's entry point. It had its own copy of this once and so
    charged nobody and attributed nothing — a credit reseller could mint free
    keys over Telegram.

    No Request: a Telegram button press is not an HTTP call that could be
    retried with the same idempotency key.
    """
    return await create_key_flow(admin, sid, CreateBody(**fields))


# --------------------------------------------------------------- subscription
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
    subcache.invalidate(token)


async def unmirror(token: str, target: str) -> None:
    """Drop `target`'s config out of a subscription. Caller checks access."""
    for m in await db.get_keys_by_sub_token(token):
        if m["server_id"] == target:
            await db.set_sub_token(target, m["key_id"], None)
    subcache.invalidate(token)   # the removed config must stop being served


async def sub_or_404(token: str, admin: dict) -> list[dict]:
    """This subscription's keys — or 404, because it is not the caller's.

    The subscription routes are keyed by `token`, not by `{sid}/{kid}`, so
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


async def sub_info(token: str, admin: dict) -> dict:
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
