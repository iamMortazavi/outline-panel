"""One backend client per configured server.

The clients satisfy `ports.node.NodePort`, so everything downstream — the
executor, the scheduler, the reconciler — talks to a server through that surface
and not to Outline specifically. A second backend (VLESS+Reality) arrives by
choosing a different adapter here, per server row, and changing nothing else.
"""

from __future__ import annotations

from ..core import config
from ..core.db import DB
from ..core.outline_api import OutlineAPI


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
            if (cur and cur["api_url"] == r["api_url"]
                    and cur.get("cert_sha256") == r.get("cert_sha256")):
                cur["name"] = r["name"]
                continue
            if cur:
                await cur["api"].close()
            self.servers[sid] = {
                **r, "api": OutlineAPI(r["api_url"], r.get("cert_sha256")),
            }

    def get(self, sid: str) -> OutlineAPI | None:
        s = self.servers.get(sid)
        return s["api"] if s else None

    def meta(self, sid: str) -> dict | None:
        return self.servers.get(sid)

    def ids(self) -> list[str]:
        return list(self.servers.keys())

    async def add(self, sid: str, name: str, api_url: str,
                  cert_sha256: str | None = None) -> None:
        await self.db.add_server(sid, name, api_url, cert_sha256)
        old = self.servers.get(sid)
        if old:  # replacing an existing entry — close its client first
            await old["api"].close()
        self.servers[sid] = {"id": sid, "name": name, "api_url": api_url,
                             "cert_sha256": cert_sha256,
                             "api": OutlineAPI(api_url, cert_sha256)}

    async def remove(self, sid: str) -> None:
        s = self.servers.pop(sid, None)
        if s:
            await s["api"].close()
        await self.db.delete_server(sid)

    async def close_all(self) -> None:
        for s in self.servers.values():
            await s["api"].close()
