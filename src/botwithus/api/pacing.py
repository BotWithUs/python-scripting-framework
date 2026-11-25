"""The agent's session plan: when the agent, not the script, decides action timing.

Mirrors the Java host's ``runtime/SessionPacing`` and ``runtime/PlannedBreak`` and the native
surface's ``bwu_session_pacing``.

When the agent's session layer is active it holds every queued action until its planned press,
and plans idle cursor runs, rests and breaks inside that hold. A delay a script adds on top of
that plan compounds with it, and the sum looks less human than either part. So a script that
paces itself should step back while :attr:`SessionPacing.active` is true:
:func:`paced_delay` returns the wait until the agent's next planned press then, and the
script's own delay otherwise.

Read-only: a script sees the plan, it never sets timing. A script that must not stall can see a
break coming in :attr:`SessionPacing.planned_break` and finish what it is doing first; the agent
never skips a break on its own.

The reply is ``get_humanizer_session``. Every time in it is an absolute time on the client's
move clock, so each one becomes a delta against the reply's own ``now_ms``, never against a
host clock. ``now_ms`` 0 means the agent could not read that clock: the deltas are then 0 and
only ``active``, ``held`` and the break's length survive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

METHOD = "get_humanizer_session"

#: The break is inside the action the agent already holds.
CARRIER_HELD_ACTION = "held_action"
#: The agent planned the next action ahead; the next action dispatched is held for this break.
CARRIER_NEXT_ACTION = "next_action"

#: Shortest delay :func:`paced_delay` returns while the agent owns timing and nothing is held:
#: long enough that a loop does not spin, short enough to add nothing beside the agent's gap.
MIN_DEFERRED_DELAY_MS = 50

_CLOCK_UNREADABLE = 0
_METHOD_NOT_FOUND = "method not found"


@dataclass(frozen=True)
class PlannedBreak:
    """A break the session layer has planned. ``starts_in_ms`` is 0 once it has started, and
    always 0 for :data:`CARRIER_NEXT_ACTION`, whose break starts when that action is
    dispatched. ``length_ms`` is the break still to run from its start (or from now)."""

    carrier: str
    starts_in_ms: int
    length_ms: int

    @property
    def ends_in_ms(self) -> int:
        return self.starts_in_ms + self.length_ms


@dataclass(frozen=True)
class SessionPacing:
    """The plan as read once. ``next_press_in_ms`` is the wait until the held action's press,
    break included, and 0 when nothing is held."""

    active: bool = False
    held: bool = False
    next_press_in_ms: int = 0
    planned_break: PlannedBreak | None = None

    @classmethod
    def from_reply(cls, reply: Any) -> "SessionPacing":
        """Decode a ``get_humanizer_session`` reply. Raises ``KeyError`` / ``TypeError`` when an
        active reply lacks its timing keys."""
        if not reply["active"]:
            return INACTIVE
        now = int(reply["now_ms"])
        held = bool(reply["held"])
        press_in = _until_or_zero(int(reply["press_ms"]), now) if held else 0
        brk = _held_break(reply, now) if held else None
        if brk is None:
            brk = _next_break(reply)
        return cls(True, held, press_in, brk)


#: The layer is absent or off: pace exactly as before it existed.
INACTIVE = SessionPacing()


def is_method_missing(agent_message: str) -> bool:
    """Whether a refusal is the agent's "method not found", i.e. it predates the session layer."""
    return agent_message.startswith(_METHOD_NOT_FOUND)


def paced_delay(pacing: SessionPacing, delay_ms: int) -> int:
    """The delay to wait: ``delay_ms`` when the agent does not own timing, else the wait until
    its next planned press, and never less than :data:`MIN_DEFERRED_DELAY_MS`."""
    if not pacing.active:
        return delay_ms
    return max(MIN_DEFERRED_DELAY_MS, pacing.next_press_in_ms)


def _until_or_zero(at: int, now: int) -> int:
    return 0 if now == _CLOCK_UNREADABLE or at <= now else at - now


def _held_break(reply: Any, now: int) -> PlannedBreak | None:
    length = int(reply["break_ms"])
    if length <= 0:
        return None
    if now == _CLOCK_UNREADABLE:
        return PlannedBreak(CARRIER_HELD_ACTION, 0, length)
    start = max(int(reply["break_start_ms"]), now)
    remaining = int(reply["break_end_ms"]) - start
    if remaining <= 0:
        return None
    return PlannedBreak(CARRIER_HELD_ACTION, start - now, remaining)


def _next_break(reply: Any) -> PlannedBreak | None:
    length = int(reply["next_break_ms"])
    return PlannedBreak(CARRIER_NEXT_ACTION, 0, length) if length > 0 else None
