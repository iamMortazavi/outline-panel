"""
Panel logins: the owner plus any sub-admins they create.

Owner-only, and not delegatable: anyone who can create an admin can create one
with every capability, so this endpoint *is* full access. The owner's own row
is editable only in the ways that cannot lock them out — their password lives
in `settings` and changes through /api/settings/password.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import security
from ..deps import CAPS, current_admin, db, reg, require_owner

router = APIRouter(prefix="/api/admins", tags=["admins"],
                   dependencies=[Depends(require_owner)])


def _public(row: dict) -> dict:
    """A row as the UI sees it — never the password hash or salt."""
    return {
        "id": row["id"],
        "username": row["username"],
        "isOwner": bool(row["is_owner"]),
        "caps": [c for c in (row["caps"] or "").split(",") if c],
        "servers": [s for s in (row["servers"] or "").split(",") if s],
        "disabled": bool(row["disabled"]),
        "createdTs": row["created_ts"],
    }


def _clean_caps(caps: list[str]) -> str:
    # An unknown slug would silently grant nothing, or something later; only
    # capabilities we actually enforce may be stored.
    bad = [c for c in caps if c not in CAPS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown capability: {bad[0]}")
    return ",".join(dict.fromkeys(caps))


def _clean_servers(servers: list[str]) -> str:
    bad = [s for s in servers if reg.meta(s) is None]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown server: {bad[0]}")
    return ",".join(dict.fromkeys(servers))


class AdminBody(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=6, max_length=200)
    caps: list[str] = []
    servers: list[str] = []


class AdminEdit(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=200)
    caps: list[str] | None = None
    servers: list[str] | None = None
    disabled: bool | None = None


@router.get("")
async def list_admins():
    return {"admins": [_public(a) for a in await db.all_admins()],
            "caps": list(CAPS),
            "servers": [{"id": s, "name": reg.meta(s)["name"]} for s in reg.ids()]}


@router.post("")
async def create_admin(body: AdminBody):
    if await db.get_admin_by_username(body.username):
        raise HTTPException(status_code=400, detail="That username is taken")
    caps = _clean_caps(body.caps)
    servers = _clean_servers(body.servers)
    if not servers:
        # An empty allowlist means "every server" (that is what the owner has).
        # Reaching that by leaving the box empty would be a silent full grant.
        raise HTTPException(status_code=400, detail="Pick at least one server")
    h, s = security.hash_password(body.password)
    aid = await db.add_admin(body.username, h, s, caps=caps, servers=servers)
    return _public(await db.get_admin(aid))


@router.put("/{admin_id}")
async def edit_admin(admin_id: int, body: AdminEdit):
    row = await db.get_admin(admin_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown admin")
    if row["is_owner"]:
        # Scoping or disabling the owner would lock the panel's only full
        # account out of its own settings; their password has its own endpoint.
        raise HTTPException(status_code=400,
                            detail="The owner's access cannot be restricted")
    fields: dict = {}
    if body.password:
        h, s = security.hash_password(body.password)
        fields.update(pw_hash=h, pw_salt=s)
    if body.caps is not None:
        fields["caps"] = _clean_caps(body.caps)
    if body.servers is not None:
        servers = _clean_servers(body.servers)
        if not servers:
            raise HTTPException(status_code=400, detail="Pick at least one server")
        fields["servers"] = servers
    if body.disabled is not None:
        fields["disabled"] = 1 if body.disabled else 0
    await db.update_admin(admin_id, **fields)
    return _public(await db.get_admin(admin_id))


@router.delete("/{admin_id}")
async def remove_admin(admin_id: int, me: dict = Depends(current_admin)):
    row = await db.get_admin(admin_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown admin")
    if row["is_owner"]:
        raise HTTPException(status_code=400, detail="The owner cannot be deleted")
    await db.delete_admin(admin_id)
    return {"ok": True}
