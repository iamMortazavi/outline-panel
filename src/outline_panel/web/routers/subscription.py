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
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ...core import config, errors
from ...core.concurrency import map_concurrently
from ...core.outline_api import OutlineError
from ...core.settings import SettingsView
from .. import subcache
from ..deps import STATIC_DIR, db, reg, settings

router = APIRouter(tags=["subscription"])


def _ss_with_label(access_url: str, label: str) -> str:
    """Clean ``ss://base64@host:port#label`` (drop Outline's /?outline=1 path)."""
    m = re.match(r"^(ss://[^@]+@[^/?#]+)", access_url or "")
    base = m.group(1) if m else (access_url or "").split("#")[0].split("?")[0]
    return f"{base}#{quote(label)}" if base else ""


def _wants_html(request: Request) -> bool:
    # ponytail: only a browser sends both a Mozilla UA and Accept: text/html — VPN
    # clients send Accept: */*. A client that spoofs both can use ?format=raw.
    fmt = request.query_params.get("format", "").lower()
    if fmt == "html":
        return True
    if fmt == "raw":
        return False
    ua = request.headers.get("user-agent", "").lower()
    return "text/html" in request.headers.get("accept", "").lower() and "mozilla" in ua


# The cache itself lives in web.subcache: the key routes have to drop an entry
# the moment they change who is in a subscription, and a router importing a
# router to do that is how the import cycle in this package began. Re-exported
# under the names this module has always used.
_cache = subcache._cache
invalidate = subcache.invalidate


async def _rate_limit(request: Request) -> None:
    """Cap fetches per address.

    This route needs no credentials and reaches every configured Outline server
    on every call, so an open loop against one link is an amplifier pointed at
    the whole fleet. The token is 22 random characters — guessing is not the
    threat; volume is.
    """
    limit = await settings.num("sub_max_per_minute")
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          if config.TRUST_PROXY else None) or \
         (request.client.host if request.client else "unknown")
    bucket = f"sub:{ip}"
    if await db.count_rate_events(bucket, 60) >= limit:
        raise errors.too_many_requests()
    await db.record_rate_event(bucket)


async def _collect(token: str) -> dict:
    """Resolve a subscription token into a usage summary (``servers[*].url`` are
    the clean ``ss://`` lines the raw sub is built from)."""
    cfg = await settings.view()
    ttl = cfg.num("sub_cache_seconds")
    if ttl:
        hit = subcache.get(token)
        if hit is not None:
            return hit
    info = await _collect_fresh(token, cfg)
    subcache.put(token, info, ttl)
    return info


async def _server_slice(sid: str, members: list[dict]) -> dict:
    """Everything one server contributes to a subscription, fetched in one go.

    The usage table is per server, so it is read once no matter how many of the
    subscription's keys live there; the key lookups then go out together rather
    than one after another.
    """
    api = reg.get(sid)
    if api is None:
        return {}
    try:
        usage = await api.get_transfer_metrics()
    except OutlineError:
        return {}

    async def one(m: dict) -> tuple[str, dict | None]:
        try:
            return m["key_id"], await api.get_key(m["key_id"])
        except OutlineError:
            return m["key_id"], None

    return {"usage": usage, "keys": dict(await map_concurrently(members, one))}


async def _collect_fresh(token: str, cfg: SettingsView | None = None) -> dict:
    await reg.sync()   # public route: no current_admin to refresh the list for us
    cfg = cfg or await settings.view()
    members = await db.get_keys_by_sub_token(token)
    if not members:
        raise errors.unknown_subscription()

    # Group the members by server, then talk to every server at once. This used
    # to walk the members one at a time, so a subscription spanning N servers
    # paid N usage reads plus N key lookups end to end — on the one endpoint
    # whose rate customers set rather than admins, since every VPN client
    # re-fetches it on its own timer. The slowest server now decides how long
    # this takes, instead of the sum of all of them.
    by_server: dict[str, list[dict]] = {}
    for m in members:
        by_server.setdefault(m["server_id"], []).append(m)
    sids = list(by_server)
    slices = dict(zip(sids, await map_concurrently(
        sids, lambda s: _server_slice(s, by_server[s])), strict=True))

    multi = len(by_server) > 1
    servers: list[dict] = []
    title = None
    download = total = expire = pending_days = 0
    any_unlimited = False

    # Rebuilt in the members' own order — created_ts — which is the order the
    # configs are handed to the client in.
    for m in members:
        sid, kid = m["server_id"], m["key_id"]
        got = slices.get(sid) or {}
        key = (got.get("keys") or {}).get(kid)
        if key is None:   # server gone, unreachable, or this key is not on it
            continue
        name = key.get("name") or m.get("name") or kid
        title = title or name
        sname = (reg.meta(sid) or {}).get("name") or sid
        line = _ss_with_label(key.get("accessUrl", ""),
                              f"{name} · {sname}" if multi else name)
        if not line:
            continue
        used = int(got["usage"].get(str(kid), 0))
        lim = m.get("limit_bytes")
        exp = m.get("expiry_ts")
        download += used
        if lim is None:
            any_unlimited = True
        else:
            total += int(lim)
        if exp:
            expire = max(expire, int(exp))
        # A plan whose clock has not started has no expiry_ts yet, which the page
        # read as "No expiry" — telling someone who bought 30 days that their
        # subscription never ends. Say what it actually is: n days, from the
        # first connection.
        if m.get("duration_days") and not m.get("activated_ts"):
            pending_days = max(pending_days, int(m["duration_days"]))
        servers.append({
            "server": sname, "used": used, "limit": lim,
            "disabled": bool(m.get("disabled")), "url": line,
        })

    # Every caller needs this: a summary with no servers is not "0 bytes of an
    # unlimited plan", it's a subscription we failed to resolve. Say so.
    if not servers:
        raise HTTPException(status_code=502, detail="No reachable server for this subscription")

    return {
        "name": title or "subscription",
        "used": download,
        "total": 0 if any_unlimited else total,
        "unlimited": any_unlimited,
        "expire": expire or 0,
        # 0 unless the validity period has not begun; then it is the term the
        # countdown will run for once the user first connects.
        "pendingDays": pending_days,
        "updateInterval": cfg.num("sub_update_hours"),
        "servers": servers,
    }


@router.get("/sub/{token}")
async def subscription(token: str, request: Request):
    # Browsers get the friendly page; VPN clients get the raw base64 sub.
    # Served before the rate limit on purpose: it is a static file that touches
    # no server, and the JSON call it then makes is limited.
    if _wants_html(request):
        return FileResponse(STATIC_DIR / "sub.html")

    await _rate_limit(request)
    info = await _collect(token)
    urls = [s["url"] for s in info["servers"]]
    payload = base64.b64encode("\n".join(urls).encode()).decode()
    userinfo = f"upload=0; download={info['used']}; total={info['total']}"
    if info["expire"]:
        userinfo += f"; expire={info['expire']}"
    headers = {
        "Subscription-Userinfo": userinfo,
        "Profile-Update-Interval": str(info["updateInterval"]),
        "Profile-Title": "base64:" + base64.b64encode(info["name"].encode()).decode(),
        "Content-Disposition": f'inline; filename="{token}"',
        "Cache-Control": "no-store",
    }
    return Response(content=payload, media_type="text/plain; charset=utf-8",
                    headers=headers)


@router.get("/sub/{token}/info")
async def subscription_info(token: str, request: Request):
    """JSON usage summary that powers the browser page (token is the secret)."""
    await _rate_limit(request)
    # Carries the same ss:// key material as the raw sub — no-store, same as it.
    return JSONResponse(await _collect(token), headers={"Cache-Control": "no-store"})
