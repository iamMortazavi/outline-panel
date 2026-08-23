"""
Carrying out what the domain decided.

The panel's tables hold **desired** state; the outbox holds the gap between
that and what each Outline server is actually enforcing. So a command always
lands in SQLite, and only the upstream half can fail — when it does, the row
goes to the outbox and a worker keeps trying.

That ordering is the whole design. The alternative, failing the operation when
one server is unreachable, means an Outline box that is down can stop you
suspending a customer on the boxes that are up. For a panel whose job is cutting
off people who have stopped paying, that is the wrong way to fail.

An upstream 404 is success, not an error: the key being gone is the state the
command was asking for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from ..core.outline_api import OutlineError
from ..domain.commands import (
    RemoveMember,
    Resume,
    SetAllowance,
    SetDuration,
    SetExpiry,
    SetMonthly,
    Suspend,
)

log = logging.getLogger("application.executor")


def encode(command) -> str:
    """A command as JSON, for the outbox. The class name is the discriminator."""
    return json.dumps({"type": type(command).__name__, **asdict(command)})


def decode(blob: str):
    """The inverse of `encode`, for the retry worker."""
    data = json.loads(blob)
    kind = data.pop("type")
    cls = {c.__name__: c for c in (Suspend, Resume, SetAllowance, SetExpiry,
                                   SetDuration, SetMonthly, RemoveMember)}[kind]
    return cls(**data)


async def apply_upstream(api, command) -> None:
    """The half that talks to an Outline server. Raises OutlineError."""
    match command:
        case Suspend():
            await api.set_data_limit(command.key_id, 0)
        case Resume(limit_bytes=None) | SetAllowance(limit_bytes=None, tell_node=True):
            await api.remove_data_limit(command.key_id)
        case Resume():
            await api.set_data_limit(command.key_id, int(command.limit_bytes))
        case SetAllowance(tell_node=True):
            await api.set_data_limit(command.key_id, int(command.limit_bytes))
        case RemoveMember():
            try:
                await api.delete_key(command.key_id)
            except OutlineError as e:
                if e.status != 404:      # already gone is the state we wanted
                    raise
        case _:
            pass                          # bookkeeping only: no upstream effect


async def apply_local(db, command) -> None:
    """The half that writes what the panel now believes."""
    sid, kid = command.server_id, command.key_id
    match command:
        case Suspend():
            await db.set_disabled(sid, kid, True)
        case Resume():
            await db.set_disabled(sid, kid, False)
        case SetAllowance():
            await db.set_limit(sid, kid, command.limit_bytes)
        case SetExpiry():
            await db.set_expiry(sid, kid, command.expiry_ts)
        case SetDuration():
            await db.set_duration(sid, kid, command.duration_days)
        case SetMonthly():
            await db.set_monthly(sid, kid, command.monthly_bytes, command.reset_ts)
        case RemoveMember():
            await db.delete_key(sid, kid)


def touches_node(command) -> bool:
    """Whether this command has an upstream effect at all.

    `SetExpiry`, `SetDuration` and a plain `SetMonthly` are bookkeeping: the
    panel remembers a date, Outline is not told anything. The distinction
    matters for the total-failure rule below — a command that never had an
    upstream half must not be counted as one that reached its server.
    """
    match command:
        case SetExpiry() | SetDuration() | SetMonthly():
            return False
        case SetAllowance(tell_node=False):
            return False
        case _:
            return True


class Executor:
    """Applies commands, and reports which members did not reach their server.

    Two failure shapes, deliberately different:

    * **Some members reached their server, some did not.** That is a partial
      success and it returns normally, with the stragglers queued. An Outline
      box that is down must not stop you suspending a customer on the boxes that
      are up — for a panel whose job is cutting off people who stopped paying,
      that is the wrong way to fail.
    * **Nothing reached any server.** Nothing converged, so nothing is written
      and nothing is queued: the operation raises and the caller sees a 502.
      This is what keeps a single-server panel behaving exactly as it did — a
      failed extend leaves the expiry alone, so a retry cannot stack the days
      twice (`test_extend_does_not_commit_before_outline`).
    """

    def __init__(self, registry, db):
        self.registry = registry
        self.db = db

    async def _attempt(self, command) -> str | None:
        """Try the upstream half. Returns None on success, else the reason."""
        api = self.registry.get(command.server_id)
        if api is None:
            return "server is not configured on this panel"
        try:
            await apply_upstream(api, command)
        except OutlineError as e:
            return str(e)
        return None

    async def run(self, commands, token: str | None = None) -> list[dict]:
        """Apply all of them. Returns one entry per member left out of step."""
        commands = tuple(commands)
        errors = {id(c): await self._attempt(c) for c in commands}

        upstream = [c for c in commands if touches_node(c)]
        if upstream and all(errors[id(c)] for c in upstream):
            # Not one member converged. Leave the panel exactly as it was.
            raise OutlineError(errors[id(upstream[0])])

        deferred: list[dict] = []
        for command in commands:
            error = errors[id(command)]
            if error is not None:
                # Queued *before* the local write, because RemoveMember deletes
                # the row the retry would otherwise be reconstructed from.
                await self.db.enqueue_command(token, command.server_id,
                                              command.key_id, encode(command),
                                              error)
                log.warning("deferred %s for %s/%s: %s",
                            type(command).__name__, command.server_id,
                            command.key_id, error)
                deferred.append({"serverId": command.server_id,
                                 "keyId": command.key_id,
                                 "op": type(command).__name__, "error": error})
            await apply_local(self.db, command)
        return deferred
