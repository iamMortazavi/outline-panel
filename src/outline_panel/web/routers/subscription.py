"""
Public subscription endpoint — no auth, the token itself is the secret.

A subscription is the set of keys that share a ``sub_token`` (so one user can
have configs on several servers under one link). The response is a base64 list
of ``ss://`` URLs (one per server) plus the standard ``Subscription-Userinfo``
and ``Profile-Update-Interval`` headers, so apps like v2rayNG / Streisand /
Shadowrocket show the remaining data + expiry and auto-refresh the link.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from ...core.outline_api import OutlineError
from ..deps import db, reg

router = APIRouter(tags=["subscription"])

# How often (hours) clients should re-fetch the subscription.
_UPDATE_INTERVAL_HOURS = 12


def _ss_with_label(access_url: str, label: str) -> str:
    """Clean ``ss://base64@host:port#label`` (drop Outline's /?outline=1 path)."""
    m = re.match(r"^(ss://[^@]+@[^/?#]+)", access_url or "")
    base = m.group(1) if m else (access_url or "").split("#")[0].split("?")[0]
    return f"{base}#{quote(label)}" if base else ""


@router.get("/sub/{token}")
async def subscription(token: str):
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise HTTPException(status_code=404, detail="Unknown subscription")

    multi = len({m["server_id"] for m in members}) > 1
    title = None  # set from the first live key name we successfully fetch

    usage_by_server: dict[str, dict] = {}
    lines: list[str] = []
    download = total = expire = 0
    any_unlimited = False

    for m in members:
        sid, kid = m["server_id"], m["key_id"]
        api = reg.get(sid)
        if api is None:
            continue
        try:
            if sid not in usage_by_server:
                usage_by_server[sid] = await api.get_transfer_metrics()
            key = await api.get_key(kid)
        except OutlineError:
            continue  # one server down shouldn't break the whole subscription
        name = key.get("name") or m.get("name") or kid
        title = title or name
        sname = (reg.meta(sid) or {}).get("name") or sid
        line = _ss_with_label(key.get("accessUrl", ""),
                              f"{name} · {sname}" if multi else name)
        if not line:
            continue
        lines.append(line)
        download += int(usage_by_server[sid].get(str(kid), 0))
        lim = m.get("limit_bytes")
        if lim is None:
            any_unlimited = True
        else:
            total += int(lim)
        if m.get("expiry_ts"):
            expire = max(expire, int(m["expiry_ts"]))

    if not lines:
        raise HTTPException(status_code=502, detail="No reachable server for this subscription")

    payload = base64.b64encode("\n".join(lines).encode()).decode()
    userinfo = f"upload=0; download={download}; total={0 if any_unlimited else total}"
    if expire:
        userinfo += f"; expire={expire}"
    headers = {
        "Subscription-Userinfo": userinfo,
        "Profile-Update-Interval": str(_UPDATE_INTERVAL_HOURS),
        "Profile-Title": "base64:" + base64.b64encode((title or "subscription").encode()).decode(),
        "Content-Disposition": f'inline; filename="{token}"',
        "Cache-Control": "no-store",
    }
    return Response(content=payload, media_type="text/plain; charset=utf-8",
                    headers=headers)
