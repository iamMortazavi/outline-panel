"""
What the panel has decided but the servers have not been told yet.

Two questions an operator could not previously ask: "is anything stuck?" and
"do my servers actually agree with this panel?". The second is the one that
matters when upgrading into the A1 fix — a panel that has been running with the
defect has customers it suspended months ago who have been connected the whole
time, and the operator should see that list before anything acts on it.

Owner-only. The report names every customer on every server, which is the whole
panel's book of business.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...application import convergence
from ..deps import db, reg, require_owner, settings

router = APIRouter(prefix="/api/convergence", tags=["convergence"],
                   dependencies=[Depends(require_owner)])


@router.get("")
async def pending():
    """Effects still queued, newest problem first by server."""
    rows = await db.due_commands(2 ** 31, limit=500)
    return {
        "depth": await db.outbox_depth(),
        "pending": [
            {"id": r["id"], "serverId": r["server_id"], "keyId": r["key_id"],
             "attempts": r["attempts"], "lastError": r["last_error"],
             "nextTryTs": r["next_try_ts"], "createdTs": r["created_ts"]}
            for r in rows
        ],
    }


@router.get("/drift")
async def drift():
    """Dry run: what each server enforces versus what this panel believes.

    Read-only, always — switching reconciliation *on* is a separate act, and a
    deliberate one, because the first pass on a panel that has been running with
    the A1 defect will cut off people who have been connected for months.
    """
    report = await convergence.reconcile(db, reg, apply=False)
    fixable = [d for d in report["differences"]
               if d["issue"] == "limit" and d.get("suspended")]
    return {
        "enabled": await settings.get_bool("reconcile_enabled", False),
        "differences": report["differences"],
        # The only category reconciliation acts on — see convergence.reconcile
        # for why a mismatched ceiling is reported but never overwritten.
        "wouldSuspend": len(fixable),
    }


@router.post("/drift")
async def apply_drift():
    """Queue the suspensions the dry run found, once, now."""
    return await convergence.reconcile(db, reg, apply=True)
