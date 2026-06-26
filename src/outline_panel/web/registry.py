"""Holds one OutlineAPI client per configured server."""

from __future__ import annotations

from .. import config
from ..db import DB
from ..outline_api import OutlineAPI


class Registry:
    def __init__(self, db: DB):
        self.db = db
        self.servers: dict[str, dict] = {}  # sid -> {id,name,api_url,cert_sha256,api}

    async def load(self) -> None:
        rows = await self.db.all_servers()
        if not rows and config.OUTLINE_API_URL:
            await self.db.add_server("default", "Server 1", config.OUTLINE_API_URL,
                                     config.OUTLINE_CERT_SHA256)
            rows = await self.db.all_servers()
        for r in rows:
            self.servers[r["id"]] = {
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
