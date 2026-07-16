"""
The price list a sub-admin sells from.

The owner defines packages; a credit-enabled admin may only pick one of these
when creating or renewing a user, and pays for it out of their balance.
Editing the list is owner-only — a package's price is the owner's revenue.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import current_admin, db, on_credit, price_for, require_owner

router = APIRouter(prefix="/api/packages", tags=["packages"])


class PackageBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    gb: float | None = Field(default=None, ge=0)      # None = unlimited data
    days: int | None = Field(default=None, ge=0)      # None/0 = no expiry
    monthly_gb: float | None = Field(default=None, ge=0)
    price: int = Field(ge=0)                          # Toman, before discount


def _public(pkg: dict, admin: dict) -> dict:
    price = price_for(pkg, admin) if on_credit(admin) else int(pkg["price"])
    out = {
        "id": pkg["id"], "name": pkg["name"],
        "gb": pkg["gb"], "days": pkg["days"], "monthlyGb": pkg["monthly_gb"],
        "price": price, "basePrice": int(pkg["price"]),
    }
    if on_credit(admin):
        # Answer "can I sell this?" here, so the picker needs no pricing logic
        # and cannot drift from what the charge will actually do.
        out["affordable"] = int(admin.get("credit") or 0) >= price
    return out


@router.get("")
async def list_packages(admin: dict = Depends(current_admin)):
    pkgs = [_public(p, admin) for p in await db.all_packages()]
    return {
        "packages": pkgs,
        "credit": int(admin.get("credit") or 0),
        "creditEnabled": on_credit(admin),
        "discountPct": int(admin.get("discount_pct") or 0),
    }


@router.post("", dependencies=[Depends(require_owner)])
async def create_package(body: PackageBody):
    pid = await db.add_package(body.name.strip(), body.gb or None,
                               body.days or None, body.price,
                               body.monthly_gb or None)
    return await db.get_package(pid)


@router.put("/{pkg_id}", dependencies=[Depends(require_owner)])
async def edit_package(pkg_id: int, body: PackageBody):
    if await db.get_package(pkg_id) is None:
        raise HTTPException(status_code=404, detail="Unknown package")
    # Editing the price does not rewrite past sales: the ledger snapshotted
    # what was charged at the time.
    await db.update_package(pkg_id, name=body.name.strip(), gb=body.gb or None,
                            days=body.days or None, price=body.price,
                            monthly_gb=body.monthly_gb or None)
    return await db.get_package(pkg_id)


@router.delete("/{pkg_id}", dependencies=[Depends(require_owner)])
async def remove_package(pkg_id: int):
    if await db.get_package(pkg_id) is None:
        raise HTTPException(status_code=404, detail="Unknown package")
    await db.delete_package(pkg_id)
    return {"ok": True}
