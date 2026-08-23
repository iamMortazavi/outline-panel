"""One backend client per configured server.

The clients satisfy `ports.node.NodePort`, so everything downstream — the
executor, the scheduler, the reconciler — talks to a server through that surface
and not to Outline specifically. A second backend (VLESS+Reality) arrives by
choosing a different adapter here, per server row, and changing nothing else.
"""

from __future__ import annotations

import json
import logging

from ..core import config
from ..core.db import DB
from ..core.outline_api import OutlineAPI

log = logging.getLogger("web.registry")


def build(row: dict):
    """The one place a server row becomes a client.

    Adding a third backend is a branch here and nothing else — every caller
    downstream talks to `ports.node.NodePort` and does not know which one it
    got. The Xray import is deferred because it pulls in `h2`, an optional
    extra: a panel with only Outline servers never imports it and does not need
    it installed.
    """
    kind = (row.get("kind") or "outline").lower()
    if kind == "outline":
        return OutlineAPI(row["api_url"], row.get("cert_sha256"))
    if kind == "xray":
        from ..core.xray.api import XrayAPI
        cfg = json.loads(row.get("config") or "{}")
        host, _, port = (row.get("api_url") or "").rpartition(":")
        return XrayAPI(host or "127.0.0.1", int(port or 10085),
                       cfg.get("inbound_tag") or "vless-in",
                       cfg.get("link") or {})
    raise ValueError(f"unknown server kind: {kind!r}")


class Registry:
    def __init__(self, db: DB):
        self.db = db
        self.servers: dict[str, dict] = {}  # sid -> {id,name,api_url,cert_sha256,api}

    async def load(self) -> None:
        if not await self.db.all_servers() and config.OUTLINE_API_URL:
            await self.db.add_server("default", "Server 1", config.OUTLINE_API_URL,
                                     config.OUTLINE_CERT_SHA256)
        await self.sync()

    async def sync(self) -> None:
        """Reconcile the in-memory map with the servers table.

        The registry used to be read once at startup and mutated in place, so a
        server added by one process — a second uvicorn worker, or the standalone
        bot — stayed invisible to every other one until a restart, and a deleted
        server stayed usable. Reading the rows is a local SQLite scan; building
        an OutlineAPI is not, so a client is rebuilt only when its url or pinned
        certificate actually moved. A rename reuses the connection pool.
        """
        rows = {r["id"]: r for r in await self.db.all_servers()}
        for sid in [s for s in self.servers if s not in rows]:
            await self.servers.pop(sid)["api"].close()
        for sid, r in rows.items():
            cur = self.servers.get(sid)
            # `or "outline"` on both sides: an entry put here directly — a
            # test double, or a row from before the column existed — has no
            # `kind`, and treating that as different from the default would
            # rebuild a client that has not changed.
            if (cur and cur["api_url"] == r["api_url"]
                    and cur.get("cert_sha256") == r.get("cert_sha256")
                    and (cur.get("kind") or "outline") == (r.get("kind") or "outline")
                    and cur.get("config") == r.get("config")):
                cur["name"] = r["name"]
                continue
            if cur:
                await cur["api"].close()
            try:
                client = build(r)
            except Exception as e:  # noqa: BLE001 — one bad row must not blank the rest
                log.error("server %s (%s) could not be built: %s",
                          sid, r.get("kind"), e)
                self.servers.pop(sid, None)
                continue
            self.servers[sid] = {**r, "api": client}

    def get(self, sid: str) -> OutlineAPI | None:
        s = self.servers.get(sid)
        return s["api"] if s else None

    def meta(self, sid: str) -> dict | None:
        return self.servers.get(sid)

    def ids(self) -> list[str]:
        return list(self.servers.keys())

    async def add(self, sid: str, name: str, api_url: str,
                  cert_sha256: str | None = None, kind: str = "outline",
                  config: str | None = None) -> None:
        await self.db.add_server(sid, name, api_url, cert_sha256, kind, config)
        old = self.servers.get(sid)
        if old:  # replacing an existing entry — close its client first
            await old["api"].close()
        row = {"id": sid, "name": name, "api_url": api_url,
               "cert_sha256": cert_sha256, "kind": kind, "config": config}
        self.servers[sid] = {**row, "api": build(row)}

    async def remove(self, sid: str) -> None:
        s = self.servers.pop(sid, None)
        if s:
            await s["api"].close()
        await self.db.delete_server(sid)

    async def close_all(self) -> None:
        for s in self.servers.values():
            await s["api"].close()
