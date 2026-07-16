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
from ..deps import STATIC_DIR, reg, settings
from . import keys as keys_router
from . import stats as stats_router
from .keys import ExtendBody, LimitBody, NameBody

router = APIRouter(tags=["miniapp"])

# The Mini App has its own membership model: a flat list of Telegram admin IDs,
# all with identical rights (require_tma below). Sub-admin scoping is a
# dashboard concept and deliberately does not reach here, so calls into the
# dashboard's key/stats functions carry a full-rights caller. Note these are
# direct function calls, so the routes' own `dependencies=[...]` never run —
# require_tma above is the whole gate.
_TMA_ADMIN = {"id": 0, "username": "telegram", "is_owner": 1,
              "caps": "", "servers": "", "disabled": 0}


async def require_tma(authorization: str | None = Header(default=None)) -> dict:
    """Authenticate a Mini App request from its Telegram ``initData`` header."""
    token = await settings.get(BOT_TOKEN)
    if not token:
        raise HTTPException(status_code=503, detail="Bot is not configured")
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing Telegram auth")
    try:
        data = security.verify_telegram_init_data(authorization[4:], token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    uid = (data.get("user") or {}).get("id")
    if uid not in await settings.get_admin_ids():
        raise HTTPException(status_code=403, detail="You are not an admin")
    return data


@router.get("/tma")
async def miniapp_page():
    return FileResponse(STATIC_DIR / "miniapp.html")


@router.get("/tma/api/bootstrap")
async def tma_bootstrap(auth: dict = Depends(require_tma)):
    user = auth.get("user") or {}
    return {
        "servers": [{"id": s, "name": reg.meta(s)["name"]} for s in reg.ids()],
        "user": {"id": user.get("id"), "name": user.get("first_name")},
    }


@router.get("/tma/api/keys")
async def tma_keys(server: str | None = None, auth: dict = Depends(require_tma)):
    return await keys_router.list_keys(server, _TMA_ADMIN)


@router.get("/tma/api/stats")
async def tma_stats(server: str | None = None, auth: dict = Depends(require_tma)):
    return await stats_router.stats(server, _TMA_ADMIN)


class TmaCreate(BaseModel):
    server: str
    name: str = Field(min_length=1, max_length=100)
    limit_gb: float = Field(ge=0, default=0)
    days: int = Field(ge=0, default=0)
    start_now: bool = False


@router.post("/tma/api/keys")
async def tma_create(body: TmaCreate, auth: dict = Depends(require_tma)):
    return await keys_router.create_key_for(
        body.server, body.name, body.limit_gb, body.days, start_now=body.start_now
    )


# --------------------------------------------------- per-key edit (reuses keys)
@router.put("/tma/api/keys/{sid}/{kid}/name")
async def tma_rename(sid: str, kid: str, body: NameBody, auth: dict = Depends(require_tma)):
    return await keys_router.rename_key(sid, kid, body)


@router.put("/tma/api/keys/{sid}/{kid}/limit")
async def tma_limit(sid: str, kid: str, body: LimitBody, auth: dict = Depends(require_tma)):
    return await keys_router.set_key_limit(sid, kid, body)


@router.post("/tma/api/keys/{sid}/{kid}/enable")
async def tma_enable(sid: str, kid: str, auth: dict = Depends(require_tma)):
    return await keys_router.enable_key(sid, kid)


@router.post("/tma/api/keys/{sid}/{kid}/disable")
async def tma_disable(sid: str, kid: str, auth: dict = Depends(require_tma)):
    return await keys_router.disable_key(sid, kid)


@router.post("/tma/api/keys/{sid}/{kid}/extend")
async def tma_extend(sid: str, kid: str, body: ExtendBody, auth: dict = Depends(require_tma)):
    return await keys_router.extend_key(sid, kid, body)


@router.delete("/tma/api/keys/{sid}/{kid}")
async def tma_delete(sid: str, kid: str, auth: dict = Depends(require_tma)):
    return await keys_router.delete_key(sid, kid)


# ----------------------------------------------------------- subscription (TMA)
@router.post("/tma/api/keys/{sid}/{kid}/sub")
async def tma_make_sub(sid: str, kid: str, auth: dict = Depends(require_tma)):
    return await keys_router.make_sub_link(sid, kid)


@router.post("/tma/api/sub/{token}/servers/{target}")
async def tma_sub_add(token: str, target: str, auth: dict = Depends(require_tma)):
    return await keys_router.sub_add_server(token, target)


@router.delete("/tma/api/sub/{token}/servers/{target}")
async def tma_sub_remove(token: str, target: str, auth: dict = Depends(require_tma)):
    return await keys_router.sub_remove_server(token, target)
