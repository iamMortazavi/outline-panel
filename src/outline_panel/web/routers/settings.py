"""Panel settings: change password, manage two-factor authentication."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import security
from ...core.settings import (
    BOT_ADMIN_IDS,
    BOT_ENABLED,
    BOT_TOKEN,
    KNOBS,
    PROFILE_BASE_URL,
    TOTP_ENABLED,
    TOTP_SECRET,
    WEBAPP_URL,
)
from ..deps import botmgr, current_admin, db, require, require_owner, settings
from ..schemas import (
    BotStatus,
    BotTested,
    Ok,
    OwnerSettings,
    PanelSaved,
    PanelSettings,
    ProfileHost,
    TotpEnrolment,
    UsernameOk,
)

# Owner-only by default, so a route added here is locked unless someone opts it
# out on purpose. The bot section is the one delegatable part, so it gets its
# own router rather than a per-route escape hatch (a router-level dependency
# cannot be relaxed further down).
router = APIRouter(prefix="/api/settings", tags=["settings"],
                   dependencies=[Depends(require_owner)])
bot_router = APIRouter(prefix="/api/settings", tags=["settings"],
                       dependencies=[Depends(require("bot.manage"))])


@router.get("", response_model=OwnerSettings, response_model_exclude_unset=True)
async def get_settings():
    return {
        "totpEnabled": await settings.get_bool(TOTP_ENABLED),
    }


# ------------------------------------------------------------- panel knobs
class ProfileBody(BaseModel):
    baseUrl: str = ""


@router.get("/profile", response_model=ProfileHost, response_model_exclude_unset=True)
async def get_profile_settings():
    base = await settings.get_profile_base()
    return {"baseUrl": base or "", "host": await settings.get_profile_host() or ""}


@router.put("/profile", response_model=ProfileHost, response_model_exclude_unset=True)
async def set_profile_settings(body: ProfileBody):
    """Set the customer profile site.

    Empty clears it, which puts the panel back to serving everything on one
    host. Anything else must be an absolute http(s) URL: the value becomes the
    hostname the guard compares against, and a bare "star.example.com" would
    parse with no hostname at all and silently gate nothing.
    """
    raw = (body.baseUrl or "").strip().rstrip("/")
    if not raw:
        await settings.set(PROFILE_BASE_URL, None)
        return {"baseUrl": "", "host": ""}
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="Enter a full URL, e.g. https://star.example.com")
    if parsed.path.strip("/"):
        raise HTTPException(status_code=400,
                            detail="Use the site root, with no path")
    await settings.set(PROFILE_BASE_URL, raw)
    return {"baseUrl": raw, "host": parsed.hostname.lower()}


@router.get("/panel", response_model=PanelSettings, response_model_exclude_unset=True)
async def get_panel_settings():
    """Current values plus the spec that describes them.

    The spec ships with the values so the settings screen is generated, not
    written: a new knob is a row in core.settings.KNOBS and nothing else.
    """
    return {
        "values": await settings.knobs(),
        "spec": [
            {"key": k, "type": v.get("type", "int"), "default": v["default"],
             "min": v.get("min"), "max": v.get("max"),
             "label": v["label"], "help": v.get("help", "")}
            for k, v in KNOBS.items()
        ],
    }


@router.put("/panel", response_model=PanelSaved, response_model_exclude_unset=True)
async def set_panel_settings(body: dict):
    """Write any subset of the knobs. Unknown keys are refused rather than
    ignored, so a typo is a visible error and not a setting that never applies."""
    for key, raw in body.items():
        spec = KNOBS.get(key)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
        if spec.get("type") == "str":
            val = str(raw).strip()
            if not val or len(val) > spec.get("max", 64):
                raise HTTPException(status_code=400,
                                    detail=f"{spec['label']}: 1–{spec.get('max', 64)} characters")
            await settings.set(key, val)
            continue
        try:
            num = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail=f"{spec['label']}: must be a whole number")
        if num < spec["min"] or num > spec["max"]:
            raise HTTPException(
                status_code=400,
                detail=f"{spec['label']}: must be between {spec['min']} and {spec['max']}")
        await settings.set(key, str(num))
    return {"ok": True, "values": await settings.knobs()}


@router.post("/panel/reset", response_model=PanelSaved, response_model_exclude_unset=True)
async def reset_panel_settings():
    """Drop every stored override and fall back to the env/spec defaults."""
    for key in KNOBS:
        await settings.set(key, None)
    return {"ok": True, "values": await settings.knobs()}


class PasswordBody(BaseModel):
    current: str
    new: str | None = Field(default=None, min_length=6, max_length=200)
    username: str | None = Field(default=None, min_length=2, max_length=40,
                                 pattern=r"^[A-Za-z0-9._-]+$")


@router.post("/password", response_model=UsernameOk, response_model_exclude_unset=True)
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


# The owner's original three. Kept for older clients, but they now write the
# same per-admin columns /api/me/2fa/* does — two places storing one secret is
# how you end up enabled in one and disabled in the other. `settings` is still
# written alongside so a rollback to the previous version still finds it.
@router.post("/2fa/start", response_model=TotpEnrolment, response_model_exclude_unset=True)
async def start_2fa(admin: dict = Depends(require_owner)):
    """Generate a fresh secret and return its provisioning URI for QR display."""
    if admin["totp_enabled"] or await settings.get_bool(TOTP_ENABLED):
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    secret = security.generate_totp_secret()
    await settings.set(TOTP_SECRET, secret)
    await db.update_admin(admin["id"], totp_secret=secret)
    return {
        "secret": secret,
        "uri": security.totp_provisioning_uri(secret, admin["username"]),
    }


class CodeBody(BaseModel):
    code: str


@router.post("/2fa/enable", response_model=Ok, response_model_exclude_unset=True)
async def enable_2fa(body: CodeBody, admin: dict = Depends(require_owner)):
    secret = admin["totp_secret"] or await settings.get(TOTP_SECRET)
    if not secret:
        raise HTTPException(status_code=400, detail="Start 2FA setup first")
    if not security.verify_totp(secret, body.code):
        raise HTTPException(status_code=400, detail="Code did not match — try again")
    await settings.set_bool(TOTP_ENABLED, True)
    await db.update_admin(admin["id"], totp_secret=secret, totp_enabled=1)
    return {"ok": True}


class PasswordOnly(BaseModel):
    password: str


@router.post("/2fa/disable", response_model=Ok, response_model_exclude_unset=True)
async def disable_2fa(body: PasswordOnly, admin: dict = Depends(require_owner)):
    if not await settings.verify_admin_password(body.password):
        raise HTTPException(status_code=401, detail="Password is wrong")
    await settings.set_bool(TOTP_ENABLED, False)
    await settings.set(TOTP_SECRET, None)
    await db.update_admin(admin["id"], totp_secret=None, totp_enabled=0)
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


@bot_router.get("/bot", response_model=BotStatus, response_model_exclude_unset=True)
async def get_bot():
    return await _bot_status()


class BotTokenBody(BaseModel):
    token: str = Field(min_length=20)


@bot_router.post("/bot/test", response_model=BotTested, response_model_exclude_unset=True)
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


@bot_router.put("/bot", response_model=BotStatus, response_model_exclude_unset=True)
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

