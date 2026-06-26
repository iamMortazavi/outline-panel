"""Access-key (user) management across all servers."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import security
from ...core.outline_api import OutlineAPI, OutlineError
from ...core.utils import gb_to_bytes
from ..deps import api_or_404, db, reg, require_session

log = logging.getLogger("web.keys")
router = APIRouter(prefix="/api", tags=["keys"],
                   dependencies=[Depends(require_session)])

_MONTH = 30 * 86400


# --------------------------------------------------------------- read helpers
async def _conn_info(api: OutlineAPI) -> dict[str, dict]:
    try:
        m = await api.get_server_metrics("30d")
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


async def keys_for_server(sid: str) -> dict:
    m = reg.meta(sid)
    api = m["api"]
    try:
        keys = await api.list_keys()
        usage = await api.get_transfer_metrics()
    except OutlineError as e:
        # Server briefly unreachable — surface the error, don't drop its keys.
        return {"serverId": sid, "serverName": m["name"], "keys": [], "error": str(e)}
    conn = await _conn_info(api)
    local = {k["key_id"]: k for k in await db.keys_for(sid)}
    out = []
    for k in keys:
        kid = k["id"]
        meta = local.get(kid, {})
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
            "resetTs": meta.get("reset_ts"),
            "subToken": meta.get("sub_token"),
            "durationDays": duration,
            "activated": activated,
            "pending": duration is not None and not activated,
            "disabled": bool(meta.get("disabled")),
            "lastSeen": c.get("lastSeen"),
            "peakDevices": c.get("peakDevices"),
            "tunnelSec": c.get("tunnelSec"),
        })
    return {"serverId": sid, "serverName": m["name"], "keys": out, "error": None}


@router.get("/keys")
async def list_keys(server: str | None = None):
    sids = [server] if server and reg.meta(server) else reg.ids()
    results = await asyncio.gather(*[keys_for_server(s) for s in sids])
    keys = [k for r in results for k in r["keys"]]
    keys.sort(key=lambda x: (x["serverName"] or "", int(x["id"]) if str(x["id"]).isdigit() else 0))
    errors = [
        {"serverId": r["serverId"], "serverName": r["serverName"], "error": r["error"]}
        for r in results if r["error"]
    ]
    return {"keys": keys, "errors": errors}


# --------------------------------------------------------------- write helpers
async def ensure_local(sid: str, kid: str) -> dict:
    meta = await db.get_key(sid, kid)
    if not meta:
        await db.add_key(sid, kid, "", None, None)
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
    limit_gb: float = Field(ge=0)
    days: int = Field(ge=0)
    monthly_gb: float = Field(ge=0, default=0)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LimitBody(BaseModel):
    limit_gb: float = Field(ge=0)


class MonthlyBody(BaseModel):
    monthly_gb: float = Field(ge=0)


class ExtendBody(BaseModel):
    days: int = Field(gt=0)


# --------------------------------------------------------------------- routes
async def create_key_for(sid: str, name: str, limit_gb: float, days: int,
                         monthly_gb: float = 0) -> dict:
    """Create a key on Outline and persist its local metadata.

    Shared by the dashboard route and the Telegram Mini App. Raises
    ``HTTPException`` on failure and removes any orphan key left on the server.
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
        raise HTTPException(status_code=502, detail=str(e))
    try:
        await db.add_key(sid, key["id"], name, limit_bytes, duration)
        if monthly_bytes:
            await db.set_monthly(sid, key["id"], monthly_bytes, int(time.time()) + _MONTH)
    except Exception as e:  # noqa: BLE001 — avoid an orphan key on the server
        log.exception("DB persist failed; deleting orphan key %s", key.get("id"))
        try:
            await api.delete_key(key["id"])
        except OutlineError:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to persist key: {e}")
    return {"id": key["id"], "serverId": sid, "name": name,
            "accessUrl": key["accessUrl"], "limit": limit_bytes,
            "monthlyBytes": monthly_bytes,
            "durationDays": duration, "pending": duration is not None}


@router.post("/servers/{sid}/keys")
async def create_key(sid: str, body: CreateBody):
    return await create_key_for(sid, body.name, body.limit_gb, body.days,
                                body.monthly_gb)


@router.put("/servers/{sid}/keys/{kid}/name")
async def rename_key(sid: str, kid: str, body: NameBody):
    api = api_or_404(sid)
    try:
        await api.rename_key(kid, body.name)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await ensure_local(sid, kid)
    await db.set_name(sid, kid, body.name)
    return {"ok": True}


@router.put("/servers/{sid}/keys/{kid}/limit")
async def set_key_limit(sid: str, kid: str, body: LimitBody):
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
            raise HTTPException(status_code=502, detail=str(e))
    await db.set_limit(sid, kid, limit_bytes)
    return {"ok": True, "limit": limit_bytes}


@router.put("/servers/{sid}/keys/{kid}/monthly")
async def set_key_monthly(sid: str, kid: str, body: MonthlyBody):
    api_or_404(sid)
    await ensure_local(sid, kid)
    if body.monthly_gb > 0:
        await db.set_monthly(sid, kid, gb_to_bytes(body.monthly_gb), int(time.time()))
    else:
        await db.set_monthly(sid, kid, None, None)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/disable")
async def disable_key(sid: str, kid: str):
    api = api_or_404(sid)
    await ensure_local(sid, kid)
    try:
        await api.set_data_limit(kid, 0)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await db.set_disabled(sid, kid, True)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/enable")
async def enable_key(sid: str, kid: str):
    api = api_or_404(sid)
    meta = await db.get_key(sid, kid)
    try:
        await enable_on_outline(api, kid, meta or {})
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await db.set_disabled(sid, kid, False)
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/extend")
async def extend_key(sid: str, kid: str, body: ExtendBody):
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)
    now = int(time.time())
    if meta.get("duration_days") is not None and meta.get("activated_ts") is None:
        await db.set_duration(sid, kid, int(meta["duration_days"]) + body.days)
    else:
        base = max(meta.get("expiry_ts") or 0, now)
        await db.set_expiry(sid, kid, base + body.days * 86400)
    if meta.get("disabled"):
        try:
            await enable_on_outline(api, kid, meta)
            await db.set_disabled(sid, kid, False)
        except OutlineError as e:
            raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@router.post("/servers/{sid}/keys/{kid}/reset")
async def reset_usage(sid: str, kid: str):
    """Give the key a fresh allowance now (used + quota), and re-enable it.

    Outline's usage counter is cumulative and can't be zeroed, so a "reset"
    raises the data limit to current-usage + the per-cycle allowance.
    """
    api = api_or_404(sid)
    meta = await ensure_local(sid, kid)
    base = meta.get("monthly_bytes") or meta.get("limit_bytes")
    if not base:
        raise HTTPException(status_code=400,
                            detail="Set a data limit or monthly quota first")
    try:
        usage = await api.get_transfer_metrics()
        used = int(usage.get(str(kid), 0))
        new_limit = used + int(base)
        await api.set_data_limit(kid, new_limit)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await db.set_limit(sid, kid, new_limit)
    await db.set_disabled(sid, kid, False)
    return {"ok": True, "limit": new_limit}


@router.post("/servers/{sid}/keys/{kid}/sub")
async def make_sub_link(sid: str, kid: str):
    """Ensure the key has a stable subscription token; return its path."""
    api_or_404(sid)
    meta = await ensure_local(sid, kid)
    token = meta.get("sub_token")
    if not token:
        token = security.random_token()
        await db.set_sub_token(sid, kid, token)
    return {"token": token, "path": f"/sub/{token}"}


@router.delete("/servers/{sid}/keys/{kid}")
async def delete_key(sid: str, kid: str):
    api = api_or_404(sid)
    try:
        await api.delete_key(kid)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await db.delete_key(sid, kid)
    return {"ok": True}
