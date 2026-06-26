"""Panel settings: change password, manage two-factor authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import security
from ...core.settings import (
    BOT_ADMIN_IDS, BOT_ENABLED, BOT_TOKEN, TOTP_ENABLED, TOTP_SECRET,
)
from ..deps import botmgr, require_session, settings

router = APIRouter(prefix="/api/settings", tags=["settings"],
                   dependencies=[Depends(require_session)])


@router.get("")
async def get_settings():
    return {
        "totpEnabled": await settings.get_bool(TOTP_ENABLED),
    }


class PasswordBody(BaseModel):
    current: str
    new: str = Field(min_length=6, max_length=200)


@router.post("/password")
async def change_password(body: PasswordBody):
    if not await settings.verify_admin_password(body.current):
        raise HTTPException(status_code=401, detail="Current password is wrong")
    await settings.set_admin_password(body.new)
    return {"ok": True}


@router.post("/2fa/start")
async def start_2fa():
    """Generate a fresh secret and return its provisioning URI for QR display."""
    if await settings.get_bool(TOTP_ENABLED):
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    secret = security.generate_totp_secret()
    await settings.set(TOTP_SECRET, secret)
    return {
        "secret": secret,
        "uri": security.totp_provisioning_uri(secret, "admin"),
    }


class CodeBody(BaseModel):
    code: str


@router.post("/2fa/enable")
async def enable_2fa(body: CodeBody):
    secret = await settings.get(TOTP_SECRET)
    if not secret:
        raise HTTPException(status_code=400, detail="Start 2FA setup first")
    if not security.verify_totp(secret, body.code):
        raise HTTPException(status_code=400, detail="Code did not match — try again")
    await settings.set_bool(TOTP_ENABLED, True)
    return {"ok": True}


class PasswordOnly(BaseModel):
    password: str


@router.post("/2fa/disable")
async def disable_2fa(body: PasswordOnly):
    if not await settings.verify_admin_password(body.password):
        raise HTTPException(status_code=401, detail="Password is wrong")
    await settings.set_bool(TOTP_ENABLED, False)
    await settings.set(TOTP_SECRET, None)
    return {"ok": True}


# ---------------------------------------------------------- Telegram bot
def _bot_status(configured: bool, enabled: bool, admin_ids: set[int]) -> dict:
    st = botmgr.status()
    return {
        "configured": configured,
        "enabled": enabled,
        "running": st["running"],
        "username": st["username"],
        "adminIds": sorted(admin_ids),
    }


@router.get("/bot")
async def get_bot():
    return _bot_status(
        configured=bool(await settings.get(BOT_TOKEN)),
        enabled=await settings.get_bool(BOT_ENABLED),
        admin_ids=await settings.get_admin_ids(),
    )


class BotTokenBody(BaseModel):
    token: str = Field(min_length=20)


@router.post("/bot/test")
async def test_bot(body: BotTokenBody):
    try:
        username = await botmgr.validate_token(body.token.strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid token: {e}")
    return {"ok": True, "username": username}


class BotBody(BaseModel):
    token: str | None = None          # omit/empty to keep the existing token
    adminIds: str = ""                # comma-separated numeric IDs
    enabled: bool = True


@router.put("/bot")
async def set_bot(body: BotBody):
    if body.token and body.token.strip():
        await settings.set(BOT_TOKEN, body.token.strip())
    ids = ",".join(x.strip() for x in body.adminIds.split(",") if x.strip().isdigit())
    await settings.set(BOT_ADMIN_IDS, ids)
    await settings.set_bool(BOT_ENABLED, body.enabled)

    token = await settings.get(BOT_TOKEN)
    try:
        if body.enabled and token:
            await botmgr.start(token)
        else:
            await botmgr.stop()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not start bot: {e}")
    return _bot_status(bool(token), body.enabled, await settings.get_admin_ids())

