"""
What a state change asks the world to do.

The domain cannot call Outline or write to SQLite, so a transition returns
these instead. Each one names a single member and a single effect, because that
is the granularity at which an Outline server can fail: a subscription on four
servers where the third is unreachable has to end up with three applied and one
recorded, never "the whole operation failed".

The split between `limit_bytes` and `tell_node` is not redundancy. A suspended
key holds its remembered ceiling in the panel while Outline is told zero, so
"what we remember" and "what the server enforces" are genuinely two values, and
collapsing them is how a resumed customer gets the wrong allowance back.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Suspend:
    """Cut this member off: Outline is told a limit of zero, the row is marked
    disabled, and the remembered ceiling is left alone so Resume can restore it."""
    server_id: str
    key_id: str


@dataclass(frozen=True, slots=True)
class Resume:
    """Put this member back on the allowance it had before it was suspended.
    `limit_bytes` None means unlimited, which is a *removal* of the data limit
    upstream rather than a large number."""
    server_id: str
    key_id: str
    limit_bytes: int | None


@dataclass(frozen=True, slots=True)
class SetAllowance:
    """Move the cumulative ceiling Outline counts against (invariant T2).

    `tell_node` is False for a suspended member: it must keep enforcing zero
    until someone resumes it, while the panel remembers the new figure.
    """
    server_id: str
    key_id: str
    limit_bytes: int | None
    tell_node: bool = True


@dataclass(frozen=True, slots=True)
class SetExpiry:
    server_id: str
    key_id: str
    expiry_ts: int | None


@dataclass(frozen=True, slots=True)
class SetDuration:
    """For a plan whose clock has not started: lengthen the term itself rather
    than a date that does not exist yet (invariant T1)."""
    server_id: str
    key_id: str
    duration_days: int


@dataclass(frozen=True, slots=True)
class SetMonthly:
    server_id: str
    key_id: str
    monthly_bytes: int | None
    reset_ts: int | None


@dataclass(frozen=True, slots=True)
class RemoveMember:
    """Delete this member's key upstream and its row here. An upstream 404 is
    success: the key being gone is the state we were asking for."""
    server_id: str
    key_id: str


Command = (Suspend | Resume | SetAllowance | SetExpiry | SetDuration
           | SetMonthly | RemoveMember)
