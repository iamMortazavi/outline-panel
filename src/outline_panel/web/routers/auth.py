"""Login / logout / session endpoints with per-IP rate limiting."""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ...core import config, security
from ...core.settings import TOTP_ENABLED, TOTP_SECRET
from ..deps import COOKIE_NAME, require_session, settings, signer

router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    password: str
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
    # honor a TLS-terminating reverse proxy (only when trusted), else the
    # request's own scheme
    proto = request.url.scheme
    if config.TRUST_PROXY:
        proto = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
                 or proto)
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
