"""Login / logout / session endpoints with per-IP rate limiting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...core import config, security
from ...core.settings import OWNER_USERNAME, TOTP_ENABLED, TOTP_SECRET
from ..deps import CAPS, COOKIE_NAME, _csv, current_admin, db, on_credit, settings, signer

router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    password: str
    username: str = OWNER_USERNAME  # older clients sent only a password
    totp: str | None = None


# --- login rate limit ------------------------------------------------------
# Per-IP throttle plus a global ceiling. The global ceiling is what protects a
# directly-exposed panel: an attacker can rotate the source IP (or, behind an
# untrusted proxy, spoof X-Forwarded-For), so the per-IP bucket alone is not
# enough — the global counter caps total failures regardless of source.
#
# Counted in the database, not in a process dict: with N workers the old version
# handed an attacker N times the budget, and which worker served a given attempt
# is not something either side controls. The scheduler prunes old rows.
_GLOBAL = "login:*"
_RATE_MSG = "Too many attempts. Try again in a few minutes."


def _client_ip(request: Request) -> str:
    # Only trust forwarded headers behind a proxy we control; otherwise they are
    # attacker-controlled and would let each request reset its own rate bucket.
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_login_rate(ip: str, max_fails: int, window: int,
                            global_max: int) -> None:
    if await db.count_rate_events(f"login:{ip}", window) >= max_fails:
        raise HTTPException(status_code=429, detail=_RATE_MSG)
    if await db.count_rate_events(_GLOBAL, window) >= global_max:
        raise HTTPException(status_code=429, detail=_RATE_MSG)


async def _record_login_fail(ip: str) -> None:
    await db.record_rate_event(f"login:{ip}")
    await db.record_rate_event(_GLOBAL)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    await _check_login_rate(ip, await settings.num("login_max_fails"),
                            await settings.num("login_window"),
                            await settings.num("login_global_max_fails"))
    admin = await settings.verify_login(body.username, body.password)
    if admin is None:
        await _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    # second factor, if enabled. The TOTP secret is the owner's, so it guards
    # the owner's login only; sub-admins have their own separate passwords.
    if admin["is_owner"] and await settings.get_bool(TOTP_ENABLED):
        secret = await settings.get(TOTP_SECRET)
        if not body.totp:
            # signal the client to prompt for a code (not a failed attempt)
            raise HTTPException(status_code=401, detail="2FA code required")
        if not security.verify_totp(secret or "", body.totp):
            await _record_login_fail(ip)
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
    await db.clear_rate_events(f"login:{ip}")
    # Name the actor for the audit trail. current_admin never runs on this
    # route, so without this a successful sign-in is recorded against nobody —
    # which reads as "not signed in signed in".
    request.state.audit_admin = admin
    # honor a TLS-terminating reverse proxy (only when trusted), else the
    # request's own scheme
    proto = request.url.scheme
    if config.TRUST_PROXY:
        proto = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
                 or proto)
    response.set_cookie(
        COOKIE_NAME, signer.dumps({"aid": admin["id"]}),
        max_age=await settings.num("session_max_age"), httponly=True,
        samesite="lax", secure=config.cookie_secure_for(proto == "https"),
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(admin: dict = Depends(current_admin)):
    # The dashboard has nothing else to branch on: it renders every control for
    # everyone unless told otherwise. This is UX, not the boundary.
    return {
        "ok": True,
        "id": admin["id"],
        "username": admin["username"],
        "isOwner": bool(admin["is_owner"]),
        "caps": list(CAPS) if admin["is_owner"] else _csv(admin["caps"]),
        "servers": _csv(admin["servers"]),
        "creditEnabled": on_credit(admin),
        "credit": int(admin["credit"] or 0),
        "discountPct": int(admin["discount_pct"] or 0),
    }


@router.get("/me/ledger")
async def my_ledger(admin: dict = Depends(current_admin)):
    """An admin is spending money; they get to see where it went."""
    return {"entries": await db.ledger_for(admin["id"])}


class MyPasswordBody(BaseModel):
    current: str
    new: str = Field(min_length=6, max_length=200)


@router.post("/me/password")
async def change_my_password(body: MyPasswordBody,
                             admin: dict = Depends(current_admin)):
    """Rotate your own password, whoever you are.

    A sub-admin had no way to: /api/settings/password is owner-only and so is
    /api/admins/{id}, so a reseller's password could only ever be set by someone
    else — and they had to be told it. verify_login is reused because it is the
    one place that knows where each kind of password lives (the owner's in
    `settings`, a sub-admin's in their row).
    """
    if await settings.verify_login(admin["username"], body.current) is None:
        raise HTTPException(status_code=401, detail="Current password is wrong")
    if admin["is_owner"]:
        await settings.set_admin_password(body.new)
    else:
        h, s = security.hash_password(body.new)
        await db.update_admin(admin["id"], pw_hash=h, pw_salt=s)
    return {"ok": True}
