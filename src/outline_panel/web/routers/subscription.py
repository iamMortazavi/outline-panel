"""Public subscription endpoint — no auth, the token itself is the secret."""

from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Response

from ...core.outline_api import OutlineError
from ..deps import db, reg

router = APIRouter(tags=["subscription"])


@router.get("/sub/{token}")
async def subscription(token: str):
    meta = await db.get_key_by_sub_token(token)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown subscription")
    api = reg.get(meta["server_id"])
    if api is None:
        raise HTTPException(status_code=404, detail="Server not available")
    try:
        key = await api.get_key(meta["key_id"])
    except OutlineError:
        raise HTTPException(status_code=502, detail="Could not reach server")
    url = key.get("accessUrl", "")
    name = key.get("name") or meta["key_id"]
    line = url if "#" in url else (f"{url}#{name}" if url else "")
    payload = base64.b64encode(line.encode()).decode()
    # standard base64 subscription body consumed by Outline/SS clients
    return Response(content=payload, media_type="text/plain; charset=utf-8")
