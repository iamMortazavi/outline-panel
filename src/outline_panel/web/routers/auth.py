"""Login / logout / session endpoints with per-IP rate limiting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ...core import config, errors, security
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
        raise errors.too_many_attempts()
    if await db.count_rate_events(_GLOBAL, window) >= global_max:
        raise errors.too_many_attempts()


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
        raise errors.bad_login()
    # Second factor, if this admin has one. It used to be read from `settings`,
    # a single global row, so it guarded the owner's login — the rare one — and
    # left every reseller, who signs in daily and spends credit, on a password
    # alone. It is now per-admin; the owner's old secret was moved into their
    # row by migration 007, and `settings` is still consulted as a fallback so a
    # panel that has not migrated yet cannot drop the owner's factor.
    secret, enabled = admin["totp_secret"], bool(admin["totp_enabled"])
    if not secret and admin["is_owner"] and await settings.get_bool(TOTP_ENABLED):
        secret, enabled = await settings.get(TOTP_SECRET), True
    if enabled and secret:
        if not body.totp:
            # signal the client to prompt for a code (not a failed attempt)
            raise errors.totp_required()
        if not security.verify_totp(secret, body.totp):
            await _record_login_fail(ip)
            raise errors.bad_totp()
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
        raise errors.wrong_password()
    if admin["is_owner"]:
        await settings.set_admin_password(body.new)
    else:
        h, s = security.hash_password(body.new)
        await db.update_admin(admin["id"], pw_hash=h, pw_salt=s)
    return {"ok": True}


# ---------------------------------------------------------- my second factor
# Enrolling is something you do to your own account, so all three routes work
# for whoever is signed in. The owner-only /api/settings/2fa/* pair stays as it
# was for older clients; both now write the same per-admin columns.
@router.get("/me/2fa")
async def my_2fa(admin: dict = Depends(current_admin)):
    return {"enabled": bool(admin["totp_enabled"]),
            "pending": bool(admin["totp_secret"] and not admin["totp_enabled"])}


@router.post("/me/2fa/start")
async def start_my_2fa(admin: dict = Depends(current_admin)):
    """Mint a secret and hand back its otpauth:// URI for the QR.

    Stored straight away but NOT enabled: the code has to be proved first, or a
    misread QR locks someone out of their own panel.
    """
    if admin["totp_enabled"]:
        raise errors.totp_already_on()
    secret = security.generate_totp_secret()
    await db.update_admin(admin["id"], totp_secret=secret)
    return {"secret": secret,
            "uri": security.totp_provisioning_uri(secret, admin["username"])}


class CodeBody(BaseModel):
    code: str


@router.post("/me/2fa/enable")
async def enable_my_2fa(body: CodeBody, admin: dict = Depends(current_admin)):
    if not admin["totp_secret"]:
        raise errors.totp_not_started()
    if not security.verify_totp(admin["totp_secret"], body.code):
        raise errors.bad_totp()
    await db.update_admin(admin["id"], totp_enabled=1)
    return {"ok": True}


class MyPasswordOnly(BaseModel):
    password: str


@router.post("/me/2fa/disable")
async def disable_my_2fa(body: MyPasswordOnly,
                         admin: dict = Depends(current_admin)):
    """Turning it off costs a password, so a borrowed open tab cannot."""
    if await settings.verify_login(admin["username"], body.password) is None:
        raise errors.wrong_password()
    await db.update_admin(admin["id"], totp_secret=None, totp_enabled=0)
    if admin["is_owner"]:
        # the pre-migration copy would otherwise switch it straight back on
        await settings.set_bool(TOTP_ENABLED, False)
        await settings.set(TOTP_SECRET, None)
    return {"ok": True}
