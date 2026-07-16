"""Panel settings: change password, manage two-factor authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import security
from ...core.settings import (
    BOT_ADMIN_IDS,
    BOT_ENABLED,
    BOT_TOKEN,
    TOTP_ENABLED,
    TOTP_SECRET,
    WEBAPP_URL,
)
from ..deps import botmgr, current_admin, db, require, require_owner, settings

# Owner-only by default, so a route added here is locked unless someone opts it
# out on purpose. The bot section is the one delegatable part, so it gets its
# own router rather than a per-route escape hatch (a router-level dependency
# cannot be relaxed further down).
router = APIRouter(prefix="/api/settings", tags=["settings"],
                   dependencies=[Depends(require_owner)])
bot_router = APIRouter(prefix="/api/settings", tags=["settings"],
                       dependencies=[Depends(require("bot.manage"))])


@router.get("")
async def get_settings():
    return {
        "totpEnabled": await settings.get_bool(TOTP_ENABLED),
    }


class PasswordBody(BaseModel):
    current: str
    new: str | None = Field(default=None, min_length=6, max_length=200)
    username: str | None = Field(default=None, min_length=2, max_length=40,
                                 pattern=r"^[A-Za-z0-9._-]+$")


@router.post("/password")
async def change_password(body: PasswordBody, admin: dict = Depends(current_admin)):
    """Change the owner's own username and/or password.

    The current password gates both: a stolen session should not be able to
    rename the account it is sitting in, let alone lock the real owner out.
    """
    if not await settings.verify_admin_password(body.current):
        raise HTTPException(status_code=401, detail="Current password is wrong")
    if body.username and body.username.lower() != admin["username"].lower():
        taken = await db.get_admin_by_username(body.username)
        if taken:
            raise HTTPException(status_code=400, detail="That username is taken")
        await db.update_admin(admin["id"], username=body.username)
    if body.new:
        await settings.set_admin_password(body.new)
    if not body.new and not body.username:
        raise HTTPException(status_code=400, detail="Nothing to change")
    return {"ok": True, "username": (await db.get_admin(admin["id"]))["username"]}


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
async def _bot_status() -> dict:
    """Read the bot's state from the store — both callers persist first."""
    st = botmgr.status()
    return {
        "configured": bool(await settings.get(BOT_TOKEN)),
        "enabled": await settings.get_bool(BOT_ENABLED),
        "running": st["running"],
        "username": st["username"],
        "adminIds": sorted(await settings.get_admin_ids()),
        "webappUrl": await settings.get_webapp_url() or "",
    }


@bot_router.get("/bot")
async def get_bot():
    return await _bot_status()


class BotTokenBody(BaseModel):
    token: str = Field(min_length=20)


@bot_router.post("/bot/test")
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
    webappUrl: str = ""               # public https base for the Mini App


@bot_router.put("/bot")
async def set_bot(body: BotBody):
    if body.token and body.token.strip():
        await settings.set(BOT_TOKEN, body.token.strip())
    ids = ",".join(x.strip() for x in body.adminIds.split(",") if x.strip().isdecimal())
    await settings.set(BOT_ADMIN_IDS, ids)
    await settings.set_bool(BOT_ENABLED, body.enabled)
    await settings.set(WEBAPP_URL, (body.webappUrl or "").strip().rstrip("/") or None)

    token = await settings.get(BOT_TOKEN)
    try:
        if body.enabled and token:
            await botmgr.start(token)
        else:
            await botmgr.stop()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not start bot: {e}")
    return await _bot_status()

