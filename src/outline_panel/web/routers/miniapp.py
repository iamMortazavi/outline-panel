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

router = APIRouter(tags=["miniapp"])


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
    return await keys_router.list_keys(server)


class TmaCreate(BaseModel):
    server: str
    name: str = Field(min_length=1, max_length=100)
    limit_gb: float = Field(ge=0, default=0)
    days: int = Field(ge=0, default=0)


@router.post("/tma/api/keys")
async def tma_create(body: TmaCreate, auth: dict = Depends(require_tma)):
    return await keys_router.create_key_for(
        body.server, body.name, body.limit_gb, body.days
    )
