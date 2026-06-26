"""Shared web state and FastAPI dependencies (auth, registry, helpers)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import Cookie, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import config
from ..bot.manager import BotManager
from ..db import DB
from ..outline_api import OutlineAPI
from ..settings import SettingsStore
from .registry import Registry

COOKIE_NAME = "outline_session"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

db = DB(config.DB_PATH)
reg = Registry(db)
settings = SettingsStore(db)
botmgr = BotManager(db, reg, settings.get_admin_ids)
signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="session")


def check_session(session: str | None) -> None:
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        signer.loads(session, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=401, detail="Session expired")


async def require_session(outline_session: str | None = Cookie(default=None)) -> None:
    """FastAPI dependency that rejects unauthenticated requests."""
    check_session(outline_session)


def api_or_404(sid: str) -> OutlineAPI:
    api = reg.get(sid)
    if api is None:
        raise HTTPException(status_code=404, detail="Unknown server")
    return api


def host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""
