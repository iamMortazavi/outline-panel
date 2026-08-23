"""
Reading the audit trail. Owner-only.

Not delegatable, for the same reason managing admins is not: an admin who could
read the log could see every other reseller's activity, and one who could prune
it could erase their own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..deps import db, require_owner
from ..schemas import AuditPage

router = APIRouter(prefix="/api/audit", tags=["audit"],
                   dependencies=[Depends(require_owner)])


@router.get("", response_model=AuditPage, response_model_exclude_unset=True)
async def list_audit(limit: int = Query(default=100, ge=1, le=500),
                     before_id: int | None = None,
                     actor_admin_id: int | None = None):
    """Newest first. Pass the last id back as `before_id` for the next page —
    keyset paging, so page 50 costs the same as page 1."""
    rows = await db.audit_page(limit=limit, before_id=before_id,
                               actor_admin_id=actor_admin_id)
    return {
        "entries": rows,
        # None when the page came back short: there is nothing more to ask for.
        "nextBeforeId": rows[-1]["id"] if len(rows) == limit else None,
    }
