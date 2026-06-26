"""Login / logout / session endpoints with per-IP rate limiting."""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ... import config, security
from ...settings import TOTP_ENABLED, TOTP_SECRET
from ..deps import COOKIE_NAME, require_session, settings, signer

router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    password: str
    totp: str | None = None


# --- simple in-memory per-IP login rate limit -----------------------------
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 300
_login_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_login_rate(ip: str) -> None:
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_fails[ip] = fails
    if len(fails) >= _LOGIN_MAX_FAILS:
        raise HTTPException(
            status_code=429, detail="Too many attempts. Try again in a few minutes."
        )


def _record_login_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.time())


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    _check_login_rate(ip)
    if not await settings.verify_admin_password(body.password):
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="Wrong password")
    # second factor, if enabled
    if await settings.get_bool(TOTP_ENABLED):
        secret = await settings.get(TOTP_SECRET)
        if not body.totp:
            # signal the client to prompt for a code (not a failed attempt)
            raise HTTPException(status_code=401, detail="2FA code required")
        if not security.verify_totp(secret or "", body.totp):
            _record_login_fail(ip)
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
    _login_fails.pop(ip, None)
    # honor a TLS-terminating reverse proxy, else the request's own scheme
    proto = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
             or request.url.scheme)
    response.set_cookie(
        COOKIE_NAME, signer.dumps(secrets.token_hex(8)),
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=config.cookie_secure_for(proto == "https"),
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_session)])
async def me():
    return {"ok": True}
