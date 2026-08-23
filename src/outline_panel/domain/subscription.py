"""
A customer is one subscription, not one key.

The panel's write endpoints are keyed by `(server_id, key_id)` because that is
what Outline understands. The *customer* is the `sub_token`: one person, one
link, and a member on each server they were sold. Every transition below acts
on all of them, which is the whole point of this module — the alternative,
proved by A1 in MODERNIZATION.md, is that suspending a customer leaves them
live on the mirror they will fail over to.

Two rules from the existing design are preserved deliberately:

* **The allowance is undivided** (invariant M8). A customer sold 40 GB on two
  servers gets 40 GB on each; a multi-server subscription is failover, not
  twice the product, and the owner absorbs the difference. So propagating a
  ceiling means giving every member the same number, never a share of it.
* **`limit_bytes` is a cumulative ceiling, not a plan size** (invariant T2).
  Outline counts up and never resets, so a renewal adds to the ceiling and a
  "reset" raises it to *this member's own* usage plus the allowance. Usage is
  counted per key upstream, so anything derived from it is per member.
"""

from __future__ import annotations

from dataclasses import dataclass

from .commands import (
    Command,
    RemoveMember,
    Resume,
    SetAllowance,
    SetDuration,
    SetExpiry,
    SetMonthly,
    Suspend,
)


@dataclass(frozen=True, slots=True)
class Grant:
    """What a package hands a customer. `None` means unlimited/no expiry, which
    is not the same as zero and must never be turned into one."""
    gb: float | None = None
    days: int | None = None
    monthly_gb: float | None = None


@dataclass(frozen=True, slots=True)
class Member:
    server_id: str
    key_id: str
    limit_bytes: int | None = None
    monthly_bytes: int | None = None
    reset_ts: int | None = None
    duration_days: int | None = None
    activated_ts: int | None = None
    expiry_ts: int | None = None
    disabled: bool = False

    @property
    def pending(self) -> bool:
        """Sold with a term whose clock has not started yet (invariant T1)."""
        return self.duration_days is not None and self.activated_ts is None

    @classmethod
    def from_row(cls, row: dict) -> Member:
        return cls(
            server_id=row["server_id"], key_id=row["key_id"],
            limit_bytes=row.get("limit_bytes"),
            monthly_bytes=row.get("monthly_bytes"),
            reset_ts=row.get("reset_ts"),
            duration_days=row.get("duration_days"),
            activated_ts=row.get("activated_ts"),
            expiry_ts=row.get("expiry_ts"),
            disabled=bool(row.get("disabled")),
        )


@dataclass(frozen=True, slots=True)
class Subscription:
    token: str
    members: tuple[Member, ...]

    @classmethod
    def from_rows(cls, token: str, rows: list[dict]) -> Subscription:
        return cls(token, tuple(Member.from_row(r) for r in rows))

    def __bool__(self) -> bool:
        return bool(self.members)

    # ------------------------------------------------------------ transitions
    def suspend(self) -> tuple[Command, ...]:
        """Stop serving this customer, everywhere. Members already suspended
        are skipped rather than re-sent: an unreachable server should not be
        retried for an effect it already has."""
        return tuple(Suspend(m.server_id, m.key_id)
                     for m in self.members if not m.disabled)

    def resume(self) -> tuple[Command, ...]:
        """Put the customer back on the allowance each member already holds."""
        return tuple(Resume(m.server_id, m.key_id, m.limit_bytes)
                     for m in self.members if m.disabled)

    def set_allowance(self, limit_bytes: int | None) -> tuple[Command, ...]:
        """One ceiling, applied to every member — see the undivided rule above."""
        return tuple(
            SetAllowance(m.server_id, m.key_id, limit_bytes,
                         tell_node=not m.disabled)
            for m in self.members)

    def set_monthly(self, monthly_bytes: int | None, first_reset_ts: int | None,
                    usage: dict[tuple[str, str], int]) -> tuple[Command, ...]:
        """Give (or clear) a per-cycle quota.

        Seeding the first cycle upstream matters: without it the quota is
        bookkeeping only and the key runs unmetered until the first scheduler
        reset, a whole cycle away. The seed is per member because it is
        `that member's usage + the allowance`.
        """
        out: list[Command] = []
        for m in self.members:
            out.append(SetMonthly(m.server_id, m.key_id, monthly_bytes,
                                  first_reset_ts if monthly_bytes else None))
            if monthly_bytes and m.limit_bytes is None:
                used = usage.get((m.server_id, m.key_id), 0)
                out.append(SetAllowance(m.server_id, m.key_id, used + monthly_bytes,
                                        tell_node=not m.disabled))
        return tuple(out)

    def reset_usage(self, usage: dict[tuple[str, str], int]) -> tuple[Command, ...]:
        """A fresh cycle now: raise each ceiling to that member's own usage plus
        its allowance, and bring the customer back online.

        Only `monthly_bytes` may be the base. `limit_bytes` is the cumulative
        ceiling this very operation writes, so using it would compound every
        cycle — 10 GB becomes 20 becomes 40.
        """
        out: list[Command] = []
        for m in self.members:
            if not m.monthly_bytes:
                continue
            used = usage.get((m.server_id, m.key_id), 0)
            out.append(SetAllowance(m.server_id, m.key_id,
                                    used + int(m.monthly_bytes), tell_node=True))
            if m.disabled:
                out.append(Resume(m.server_id, m.key_id, used + int(m.monthly_bytes)))
        return tuple(out)

    def extend(self, days: int, now: int) -> tuple[Command, ...]:
        """Move every member's clock by `days` — positive renews, negative
        shortens (clamped to expire-now). A renewal re-enables; a reduction
        never does."""
        out: list[Command] = []
        for m in self.members:
            if m.pending:
                # the term has not started, so lengthen the term itself
                out.append(SetDuration(m.server_id, m.key_id,
                                       max(1, int(m.duration_days) + days)))
            else:
                base = max(m.expiry_ts or 0, now)
                out.append(SetExpiry(m.server_id, m.key_id,
                                     max(now, base + days * 86400)))
            if days > 0 and m.disabled:
                out.append(Resume(m.server_id, m.key_id, m.limit_bytes))
        return tuple(out)

    def apply_grant(self, grant: Grant, now: int,
                    gb_to_bytes) -> tuple[Command, ...]:
        """Add a package's time and volume to every member (a renewal).

        Unlimited stays unlimited in both directions: if either the package or
        the member is unlimited the result is, because taking access away is
        never what buying more means.
        """
        out: list[Command] = []
        for m in self.members:
            if grant.gb is None or m.limit_bytes is None:
                new_limit = None
            else:
                new_limit = int(m.limit_bytes) + gb_to_bytes(grant.gb)
            out.append(SetAllowance(m.server_id, m.key_id, new_limit,
                                    tell_node=True))
            days = int(grant.days or 0)
            if days:
                if m.pending:
                    out.append(SetDuration(m.server_id, m.key_id,
                                           int(m.duration_days) + days))
                else:
                    base = max(m.expiry_ts or 0, now)
                    out.append(SetExpiry(m.server_id, m.key_id, base + days * 86400))
            if m.disabled:
                out.append(Resume(m.server_id, m.key_id, new_limit))
        return tuple(out)

    def remove(self) -> tuple[Command, ...]:
        """Delete the customer. Every member, or the ones left behind are live
        configs with no panel row to find them by."""
        return tuple(RemoveMember(m.server_id, m.key_id) for m in self.members)
