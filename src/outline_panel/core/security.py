"""
Password hashing and TOTP (RFC 6238) — both stdlib-only (no extra deps).

Password: hashlib.scrypt with a random per-password salt, stored as hex.
TOTP: standard 30-second SHA1 6-digit codes, compatible with Google
Authenticator / Authy and Outline's existing in-page QR renderer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from urllib.parse import parse_qsl, quote

# scrypt parameters (interactive-login appropriate)
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


# ----------------------------------------------------------------- passwords
def hash_password(password: str) -> tuple[str, str]:
    """Return (hash_hex, salt_hex)."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return dk.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    if not hash_hex or not salt_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---------------------------------------------------------------------- TOTP
def generate_totp_secret() -> str:
    """Base32 secret (no padding) suitable for authenticator apps."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_now(secret_b32: str, step: int = 30) -> str:
    return _hotp(secret_b32, int(time.time()) // step)


def verify_totp(secret_b32: str, code: str, step: int = 30, window: int = 1) -> bool:
    if not code or not secret_b32:
        return False
    code = code.strip().replace(" ", "")
    counter = int(time.time()) // step
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + drift), code):
            return True
    return False


def totp_provisioning_uri(secret_b32: str, account: str, issuer: str = "Outline Panel") -> str:
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits=6&period=30")


def random_token(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


# ------------------------------------------------ Telegram Mini App (Web App)
def verify_telegram_init_data(
    init_data: str, bot_token: str, max_age: int = 86400
) -> dict:
    """Validate a Telegram Mini App ``initData`` string and return its fields.

    Implements Telegram's documented check: every field except ``hash`` is
    sorted into a newline-joined ``key=value`` string, signed with
    ``HMAC-SHA256(key=HMAC-SHA256("WebAppData", bot_token), msg=check_string)``
    and compared against the provided ``hash``. The ``user`` field is decoded
    from JSON. Raises ``ValueError`` on a missing/bad signature or stale
    ``auth_date`` (older than ``max_age`` seconds; pass 0 to skip the age check).
    """
    if not init_data or not bot_token:
        raise ValueError("missing init data or bot token")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", None)
    if not received:
        raise ValueError("no hash in init data")
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad signature")
    if max_age:
        try:
            auth_date = int(data.get("auth_date", "0") or 0)
        except ValueError:
            auth_date = 0
        if auth_date <= 0 or (time.time() - auth_date) > max_age:
            raise ValueError("init data expired")
    if data.get("user"):
        try:
            data["user"] = json.loads(data["user"])
        except (ValueError, TypeError):
            data["user"] = {}
    return data
