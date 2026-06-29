"""
Public subscription endpoint — no auth, the token itself is the secret.

A subscription is the set of keys that share a ``sub_token`` (so one user can
have configs on several servers under one link). The same URL serves two shapes
by content negotiation:

* VPN clients (v2rayNG / Clash / sing-box / Streisand …) get a base64 list of
  ``ss://`` URLs plus the standard ``Subscription-Userinfo`` /
  ``Profile-Update-Interval`` headers, so the app shows remaining data + expiry
  and auto-refreshes.
* A web browser gets a human-friendly page (``static/sub.html``) that shows the
  same usage, expiry and per-server configs with copy/QR — no app required.
"""

from __future__ import annotations

import base64
import re
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ...core.outline_api import OutlineError
from ..deps import STATIC_DIR, db, reg

router = APIRouter(tags=["subscription"])

# How often (hours) clients should re-fetch the subscription.
_UPDATE_INTERVAL_HOURS = 12

# User-Agent fragments of known VPN clients — they must always get the raw sub.
_CLIENT_UAS = (
    "v2ray", "clash", "sing-box", "singbox", "shadowrocket", "streisand",
    "nekobox", "nekoray", "hiddify", "outline", "surfboard", "quantumult",
    "loon", "stash", "v2box", "foxray", "karing", "sn-proxy", "passwall",
)


def _ss_with_label(access_url: str, label: str) -> str:
    """Clean ``ss://base64@host:port#label`` (drop Outline's /?outline=1 path)."""
    m = re.match(r"^(ss://[^@]+@[^/?#]+)", access_url or "")
    base = m.group(1) if m else (access_url or "").split("#")[0].split("?")[0]
    return f"{base}#{quote(label)}" if base else ""


def _wants_html(request: Request) -> bool:
    fmt = request.query_params.get("format", "").lower()
    if fmt in ("html", "page"):
        return True
    if fmt in ("raw", "sub", "base64", "txt"):
        return False
    ua = request.headers.get("user-agent", "").lower()
    if any(c in ua for c in _CLIENT_UAS):
        return False
    return "text/html" in request.headers.get("accept", "").lower() and "mozilla" in ua


async def _collect(token: str) -> dict:
    """Resolve a subscription token into clean ss:// lines + a usage summary."""
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise HTTPException(status_code=404, detail="Unknown subscription")

    multi = len({m["server_id"] for m in members}) > 1
    usage_by_server: dict[str, dict] = {}
    lines: list[str] = []
    servers: list[dict] = []
    title = None
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
            continue
        name = key.get("name") or m.get("name") or kid
        title = title or name
        sname = (reg.meta(sid) or {}).get("name") or sid
        line = _ss_with_label(key.get("accessUrl", ""),
                              f"{name} · {sname}" if multi else name)
        if not line:
            continue
        lines.append(line)
        used = int(usage_by_server[sid].get(str(kid), 0))
        lim = m.get("limit_bytes")
        exp = m.get("expiry_ts")
        download += used
        if lim is None:
            any_unlimited = True
        else:
            total += int(lim)
        if exp:
            expire = max(expire, int(exp))
        servers.append({
            "server": sname, "name": name, "used": used, "limit": lim,
            "expiry": exp, "disabled": bool(m.get("disabled")), "url": line,
        })

    return {
        "lines": lines,
        "info": {
            "name": title or "subscription",
            "used": download,
            "total": 0 if any_unlimited else total,
            "unlimited": any_unlimited,
            "expire": expire or 0,
            "updateInterval": _UPDATE_INTERVAL_HOURS,
            "now": int(time.time()),
            "servers": servers,
        },
    }


@router.get("/sub/{token}")
async def subscription(token: str, request: Request):
    # Browsers get the friendly page; VPN clients get the raw base64 sub.
    if _wants_html(request):
        return FileResponse(STATIC_DIR / "sub.html")

    data = await _collect(token)
    if not data["lines"]:
        raise HTTPException(status_code=502, detail="No reachable server for this subscription")
    info = data["info"]
    payload = base64.b64encode("\n".join(data["lines"]).encode()).decode()
    userinfo = f"upload=0; download={info['used']}; total={info['total']}"
    if info["expire"]:
        userinfo += f"; expire={info['expire']}"
    headers = {
        "Subscription-Userinfo": userinfo,
        "Profile-Update-Interval": str(_UPDATE_INTERVAL_HOURS),
        "Profile-Title": "base64:" + base64.b64encode(info["name"].encode()).decode(),
        "Content-Disposition": f'inline; filename="{token}"',
        "Cache-Control": "no-store",
    }
    return Response(content=payload, media_type="text/plain; charset=utf-8",
                    headers=headers)


@router.get("/sub/{token}/info")
async def subscription_info(token: str):
    """JSON usage summary that powers the browser page (token is the secret)."""
    return (await _collect(token))["info"]
