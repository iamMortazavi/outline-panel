"""
Telegram Mini App (Web App) — a phone-friendly panel opened from the bot.

Authentication is **not** the dashboard session cookie. Every request carries
the Telegram ``initData`` blob in an ``Authorization: tma <initData>`` header;
``require_tma`` validates its HMAC signature against the configured bot token
and checks the Telegram user is one of the bot admins. So the same people who
can use the bot can use the Mini App, with zero extra credentials.

The Mini App deliberately exposes only what the bot does: list keys with their
usage, and create a new key (picking a server when more than one is configured).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ...core import security
from ...core.settings import BOT_TOKEN
from ..deps import (
    CAPS,
    STATIC_DIR,
    _csv,
    admin_for_telegram,
    assert_cap,
    assert_key_access,
    on_credit,
    reg,
    scoped_ids,
    settings,
)
from . import keys as keys_router
from . import packages as packages_router
from . import stats as stats_router
from .keys import ExtendBody, LimitBody, NameBody

router = APIRouter(tags=["miniapp"])

async def require_tma(authorization: str | None = Header(default=None)) -> dict:
    """Authenticate a Mini App request and resolve who is calling.

    Returns the panel admin row with an extra ``_tg`` key holding the Telegram
    user. The Mini App used to hand every caller full rights; now it carries the
    same identity the dashboard would, so caps, server scope, ownership and
    credit all apply here too. These routes call the key functions directly, so
    their `dependencies=[...]` never run — the assert_* calls below are the gate.
    """
    token = await settings.get(BOT_TOKEN)
    if not token:
        raise HTTPException(status_code=503, detail="Bot is not configured")
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing Telegram auth")
    try:
        data = security.verify_telegram_init_data(authorization[4:], token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    user = data.get("user") or {}
    admin = await admin_for_telegram(user.get("id"))
    if admin is None:
        raise HTTPException(status_code=403, detail="You are not an admin")
    return {**admin, "_tg": user}


@router.get("/tma")
async def miniapp_page():
    return FileResponse(STATIC_DIR / "miniapp.html")


@router.get("/tma/api/bootstrap")
async def tma_bootstrap(auth: dict = Depends(require_tma)):
    user = auth.get("_tg") or {}
    # the app renders from this, so it must know what this admin may do
    return {
        "servers": [{"id": s, "name": reg.meta(s)["name"]} for s in scoped_ids(auth)],
        "user": {"id": user.get("id"), "name": user.get("first_name")},
        "me": {
            "username": auth["username"],
            "isOwner": bool(auth["is_owner"]),
            "caps": list(CAPS) if auth["is_owner"] else _csv(auth["caps"]),
            "creditEnabled": on_credit(auth),
            "credit": int(auth["credit"] or 0),
        },
    }


@router.get("/tma/api/keys")
async def tma_keys(server: str | None = None, auth: dict = Depends(require_tma)):
    assert_cap(auth, "keys.view")
    return await keys_router.list_keys(server, auth)


@router.get("/tma/api/stats")
async def tma_stats(server: str | None = None, auth: dict = Depends(require_tma)):
    assert_cap(auth, "keys.view")
    return await stats_router.stats(server, auth)


@router.get("/tma/api/packages")
async def tma_packages(auth: dict = Depends(require_tma)):
    return await packages_router.list_packages(auth)


class TmaCreate(BaseModel):
    server: str
    name: str = Field(min_length=1, max_length=100)
    limit_gb: float = Field(ge=0, default=0)
    days: int = Field(ge=0, default=0)
    start_now: bool = False
    package_id: int | None = None


@router.post("/tma/api/keys")
async def tma_create(body: TmaCreate, auth: dict = Depends(require_tma)):
    assert_cap(auth, "keys.create")
    await assert_key_access(auth, body.server)
    # Reuse the dashboard's route body, not create_key_for: it is what charges a
    # credit admin and reverses on failure. Free-form creation from Telegram
    # would let a reseller mint keys around the price list entirely.
    return await keys_router.create_key(
        body.server,
        keys_router.CreateBody(name=body.name, limit_gb=body.limit_gb,
                               days=body.days, start_now=body.start_now,
                               package_id=body.package_id),
        auth,
    )


async def _may(admin: dict, cap: str, sid: str, kid: str | None = None) -> None:
    """One line per route instead of a dependency, because these call the key
    functions directly and FastAPI's dependencies never fire on that path."""
    assert_cap(admin, cap)
    await assert_key_access(admin, sid, kid)


# --------------------------------------------------- per-key edit (reuses keys)
@router.put("/tma/api/keys/{sid}/{kid}/name")
async def tma_rename(sid: str, kid: str, body: NameBody, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.rename_key(sid, kid, body)


@router.put("/tma/api/keys/{sid}/{kid}/limit")
async def tma_limit(sid: str, kid: str, body: LimitBody, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.set_key_limit(sid, kid, body)


@router.post("/tma/api/keys/{sid}/{kid}/enable")
async def tma_enable(sid: str, kid: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.enable_key(sid, kid)


@router.post("/tma/api/keys/{sid}/{kid}/disable")
async def tma_disable(sid: str, kid: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.disable_key(sid, kid)


@router.post("/tma/api/keys/{sid}/{kid}/extend")
async def tma_extend(sid: str, kid: str, body: ExtendBody, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.extend_key(sid, kid, body, auth)


@router.delete("/tma/api/keys/{sid}/{kid}")
async def tma_delete(sid: str, kid: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.delete", sid, kid)
    return await keys_router.delete_key(sid, kid)


# ----------------------------------------------------------- subscription (TMA)
@router.post("/tma/api/keys/{sid}/{kid}/sub")
async def tma_make_sub(sid: str, kid: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", sid, kid)
    return await keys_router.make_sub_link(sid, kid, auth)


@router.post("/tma/api/sub/{token}/servers/{target}")
async def tma_sub_add(token: str, target: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", target)
    return await keys_router.sub_add_server(token, target, auth)


@router.delete("/tma/api/sub/{token}/servers/{target}")
async def tma_sub_remove(token: str, target: str, auth: dict = Depends(require_tma)):
    await _may(auth, "keys.edit", target)
    return await keys_router.sub_remove_server(token, target, auth)
