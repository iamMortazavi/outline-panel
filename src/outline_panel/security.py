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
import os
import secrets
import struct
import time
from urllib.parse import quote

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
