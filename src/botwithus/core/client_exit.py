"""The one rule for "the attached client has exited", shared by the worker loop and the
script context's waits.

After the client process exits, the native host keeps its view of the snapshot mapped: the
reads keep returning the last frame, so ``server_tick`` simply stops moving and nothing in a
read says the client is gone. Two things do: ``refresh`` turns False, and the pid drops out
of discovery. The client counts as gone only when both hold on two looks at least
:data:`CLIENT_GONE_CONFIRM_S` apart, so one discovery flap (a process scan racing the pipe)
cannot end a run. This is the Python form of Lua's ``not refresh() and not is_attached()``.

A connection the host has already marked dead (an RPC failed with a transport error such as
"client exited") counts as gone at once.
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

# Two looks at least this far apart must both miss the client. Read at call time, so a test
# may shorten it.
CLIENT_GONE_CONFIRM_S = 1.0
# A remembered first miss older than this many confirm times is dropped: the next miss starts
# over, so a stale miss (say, from a sleep long ago, with no look since) cannot pair with a
# later one into a confirmation.
FIRST_MISS_STALE_FACTOR = 2.0


class _Watched(Protocol):
    """What the rule needs from a connection (``core.connection.Connection``)."""

    @property
    def alive(self) -> bool: ...

    def snapshot_readable(self) -> bool: ...

    def client_listed(self) -> bool: ...


class ClientExitWatch:
    """Applies the client-gone rule to one connection, remembering the first miss.

    :meth:`look` never blocks, so a wait loop can call it on every poll; :meth:`confirm`
    blocks for the rest of the confirm time when a look missed. Both cost one ``refresh``
    while the client is up: discovery is asked only after ``refresh`` has failed."""

    __slots__ = ("_conn", "_first_miss")

    def __init__(self, conn: _Watched) -> None:
        self._conn = conn
        self._first_miss: float | None = None

    def _unreachable(self) -> bool:
        """One look: no fresh frame (``refresh`` False) and the pid not discovered."""
        return not self._conn.snapshot_readable() and not self._conn.client_listed()

    def look(self) -> bool:
        """True once the client is confirmed gone. A reachable look forgets an earlier miss,
        and so does a miss older than :data:`FIRST_MISS_STALE_FACTOR` confirm times."""
        return self._look(drop_stale=True)

    def _look(self, drop_stale: bool) -> bool:
        if not self._conn.alive:
            return True
        if not self._unreachable():
            self._first_miss = None
            return False
        now = time.monotonic()
        stale_after = FIRST_MISS_STALE_FACTOR * CLIENT_GONE_CONFIRM_S
        if self._first_miss is None or (drop_stale and now - self._first_miss > stale_after):
            self._first_miss = now
            return False
        return now - self._first_miss >= CLIENT_GONE_CONFIRM_S

    def confirm(self, stop: threading.Event) -> bool:
        """:meth:`look`, and after a miss, wait out the confirm time and look again. The wait
        ends early on ``stop`` (False: the caller then sees the stop and ends as stopped)."""
        if self.look():
            return True
        first_miss = self._first_miss
        if first_miss is None:
            return False
        while (left := CLIENT_GONE_CONFIRM_S - (time.monotonic() - first_miss)) > 0:
            if stop.wait(left):
                return False
        # The deliberate second look of this pair: an oversleep on a loaded machine must not
        # make its own first miss look stale.
        return self._look(drop_stale=False)
