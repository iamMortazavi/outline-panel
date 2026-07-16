"""Login / logout / session endpoints with per-IP rate limiting."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ...core import config, security
from ...core.settings import OWNER_USERNAME, TOTP_ENABLED, TOTP_SECRET
from ..deps import CAPS, COOKIE_NAME, _csv, current_admin, db, on_credit, settings, signer

router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    password: str
    username: str = OWNER_USERNAME  # older clients sent only a password
    totp: str | None = None


# --- simple in-memory login rate limit ------------------------------------
# Per-IP throttle plus a global ceiling. The global ceiling is what protects a
# directly-exposed panel: an attacker can rotate the source IP (or, behind an
# untrusted proxy, spoof X-Forwarded-For), so the per-IP bucket alone is not
# enough — the global counter caps total failures regardless of source.
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 300
_GLOBAL_MAX_FAILS = 30
_login_fails: dict[str, list[float]] = {}
_global_fails: list[float] = []
_RATE_MSG = "Too many attempts. Try again in a few minutes."


def _client_ip(request: Request) -> str:
    # Only trust forwarded headers behind a proxy we control; otherwise they are
    # attacker-controlled and would let each request reset its own rate bucket.
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_login_rate(ip: str) -> None:
    now = time.time()
    global _global_fails
    _global_fails = [t for t in _global_fails if now - t < _LOGIN_WINDOW]
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    # Only keep IPs that actually owe us something. Storing a bucket for every
    # IP we merely *saw* grew this dict forever — one entry per source address,
    # even for requests already being 429'd, which is free memory for a botnet.
    # The global ceiling bounds recorded fails per window, so this stays small.
    if fails:
        _login_fails[ip] = fails
    else:
        _login_fails.pop(ip, None)
    if len(fails) >= _LOGIN_MAX_FAILS or len(_global_fails) >= _GLOBAL_MAX_FAILS:
        raise HTTPException(status_code=429, detail=_RATE_MSG)


def _record_login_fail(ip: str) -> None:
    now = time.time()
    _login_fails.setdefault(ip, []).append(now)
    _global_fails.append(now)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    _check_login_rate(ip)
    admin = await settings.verify_login(body.username, body.password)
    if admin is None:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    # second factor, if enabled. The TOTP secret is the owner's, so it guards
    # the owner's login only; sub-admins have their own separate passwords.
    if admin["is_owner"] and await settings.get_bool(TOTP_ENABLED):
        secret = await settings.get(TOTP_SECRET)
        if not body.totp:
            # signal the client to prompt for a code (not a failed attempt)
            raise HTTPException(status_code=401, detail="2FA code required")
        if not security.verify_totp(secret or "", body.totp):
            _record_login_fail(ip)
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
    _login_fails.pop(ip, None)
    # honor a TLS-terminating reverse proxy (only when trusted), else the
    # request's own scheme
    proto = request.url.scheme
    if config.TRUST_PROXY:
        proto = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
                 or proto)
    response.set_cookie(
        COOKIE_NAME, signer.dumps({"aid": admin["id"]}),
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=config.cookie_secure_for(proto == "https"),
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
