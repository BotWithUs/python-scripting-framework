"""Condition waits, random delays, and the timing helpers scripts keep re-writing.

Mirrors the Java host's ``util/Timing``, ``util/ConditionWait``, ``util/Stopwatch``,
``util/Timer`` and ``util/Durations`` (JBotWithUsV2, phase J1/J3).

**Condition waits.** :func:`delay_until`, :func:`delay_while`, :func:`delay_until_ticks` and
:func:`delay_ticks` return whether the condition was met. They poll every
:data:`POLL_INTERVAL_MS` on the calling thread and look for a stop before every poll, so a
stopping script leaves the wait within one poll, not at the timeout. While the script is paused
(:meth:`ScriptContext.pause`) they neither test the condition nor spend their timeout or tick
budget.

They find the calling script's stop and pause switches through the script thread they run on:
the runner binds its :class:`~botwithus.api.script.ScriptContext` around the lifecycle. Called
from a thread no script owns, only the process-wide stop (:func:`botwithus.core.ctl.stopping`)
ends them early and nothing pauses them. ``ctx.delay_until(...)`` and friends are the same waits
with the context passed explicitly.

The tick forms count ``server_tick`` (the 600 ms server step), never ``game_cycle`` or
``publish_seq``. When no tick is advancing (logged out, no frame) they give up after
:data:`STALLED_CLOCK_FACTOR` tick lengths of wall time per tick, so a stalled clock cannot hang a
script.
"""
from __future__ import annotations

import random as _random
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core import ctl as _ctl

if TYPE_CHECKING:
    from .script import ScriptContext

TICK_MS = 600
POLL_INTERVAL_MS = 50
STALLED_CLOCK_FACTOR = 2
NO_TICK: int | None = None

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
_MS_PER_HOUR = 3_600_000
_NS_PER_MS = 1_000_000

Condition = Callable[[], bool]

# The script context bound to the current thread by the runner. A threading.local, so each
# script thread sees only its own: the Python form of the Java host's ScopedValue binding.
_bound = threading.local()


class bound_context:
    """``with bound_context(ctx):`` binds ``ctx`` as the current thread's script context, so the
    module-level waits find its stop and pause switches. Used by the runners; restores the
    previous binding on exit."""

    def __init__(self, ctx: "ScriptContext | None") -> None:
        self._ctx = ctx
        self._previous: Any = None

    def __enter__(self) -> "ScriptContext | None":
        self._previous = getattr(_bound, "ctx", None)
        _bound.ctx = self._ctx
        return self._ctx

    def __exit__(self, *exc: object) -> None:
        _bound.ctx = self._previous


def current_context() -> "ScriptContext | None":
    """The script context the runner bound to this thread, or None off a script thread."""
    return getattr(_bound, "ctx", None)


def ticks_to_ms(ticks: int) -> int:
    return ticks * TICK_MS


def random_between(low: int, high: int, rng: _random.Random | None = None) -> int:
    """A uniform int in ``low..high`` inclusive; ``low`` when ``low >= high``."""
    if low >= high:
        return low
    return (rng or _random).randint(low, high)


def gaussian_random(mean: float, std_dev: float, rng: _random.Random | None = None) -> int:
    """A normal draw around ``mean``, never below 0."""
    return max(0, int((rng or _random).gauss(mean, std_dev)))


class ConditionWait:
    """The engine behind the waits. Its clock (ns), sleeper (ms), stop and pause switches are
    constructor arguments, which is what the tests drive."""

    def __init__(self, nano_clock: Callable[[], int], sleeper: Callable[[int], None],
                 stop_requested: Callable[[], bool],
                 paused: Callable[[], bool] = lambda: False) -> None:
        self._now = nano_clock
        self._sleep = sleeper
        self._stopped = stop_requested
        self._paused = paused

    @classmethod
    def for_context(cls, ctx: "ScriptContext | None") -> "ConditionWait":
        """The wait for ``ctx`` (or the thread's bound context when None)."""
        ctx = ctx if ctx is not None else current_context()
        if ctx is None:
            return cls(time.monotonic_ns, _sleep_ms, _ctl.stopping)
        return cls(time.monotonic_ns, _sleep_ms, ctx.is_stop_requested, lambda: ctx.is_paused)

    def until(self, condition: Condition, timeout_ms: int) -> bool:
        deadline = self._now() + max(0, timeout_ms) * _NS_PER_MS
        while not self._stopped():
            held = self._hold_while_paused()
            if held > 0:
                deadline += held
                continue
            if condition():
                return True
            remaining_ms = (deadline - self._now()) // _NS_PER_MS
            if remaining_ms <= 0:
                return False
            self._sleep(min(POLL_INTERVAL_MS, remaining_ms))
        return False

    def until_ticks(self, server_tick: Callable[[], int | None], condition: Condition,
                    max_ticks: int) -> bool:
        return self._run_ticks(server_tick, condition, max_ticks, budget_result=False)

    def ticks(self, server_tick: Callable[[], int | None], ticks: int) -> bool:
        return self._run_ticks(server_tick, lambda: False, ticks, budget_result=True)

    def _run_ticks(self, server_tick: Callable[[], int | None], condition: Condition,
                   max_ticks: int, budget_result: bool) -> bool:
        start = server_tick()
        cap_ticks = max_ticks + 1 if budget_result else max_ticks
        wall_deadline = self._now() + _wall_cap_ns(cap_ticks)
        elapsed_before_hold = 0
        while not self._stopped():
            before_hold = server_tick()
            held = self._hold_while_paused()
            if held > 0:
                # Ticks that passed while paused do not count against the budget.
                elapsed_before_hold += _ticks_elapsed(before_hold, server_tick())
                wall_deadline += held
                continue
            if condition():
                return True
            if _ticks_elapsed(start, server_tick()) - elapsed_before_hold >= max_ticks:
                return budget_result
            if self._now() - wall_deadline >= 0:
                return False
            self._sleep(POLL_INTERVAL_MS)
        return False

    def _hold_while_paused(self) -> int:
        """Block while paused; the ns held (at least 1 when it held at all), else 0."""
        if not self._paused():
            return 0
        start = self._now()
        while self._paused() and not self._stopped():
            self._sleep(POLL_INTERVAL_MS)
        return max(1, self._now() - start)


def _ticks_elapsed(start: int | None, now: int | None) -> int:
    if start is None or now is None:
        return 0
    return now - start


def _wall_cap_ns(max_ticks: int) -> int:
    return ticks_to_ms(max(1, max_ticks)) * STALLED_CLOCK_FACTOR * _NS_PER_MS


def _sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


def _server_tick_of(game: Any) -> Callable[[], int | None]:
    def read() -> int | None:
        tick = game.snapshot().server_tick
        return tick if tick >= 0 else NO_TICK
    return read


def delay_until(condition: Condition, timeout_ms: int, *,
                ctx: "ScriptContext | None" = None) -> bool:
    """Poll ``condition`` until it is true (True) or ``timeout_ms`` passes (False).

    False at once on a stop, and within one poll of a stop that comes during the wait."""
    return ConditionWait.for_context(ctx).until(condition, timeout_ms)


def delay_while(condition: Condition, timeout_ms: int, *,
                ctx: "ScriptContext | None" = None) -> bool:
    """Poll until ``condition`` is false (True) or ``timeout_ms`` passes (False)."""
    return delay_until(lambda: not condition(), timeout_ms, ctx=ctx)


def delay_until_ticks(game: Any, condition: Condition, max_ticks: int, *,
                      ctx: "ScriptContext | None" = None) -> bool:
    """Poll ``condition`` for up to ``max_ticks`` server ticks of ``game``."""
    return ConditionWait.for_context(ctx).until_ticks(_server_tick_of(game), condition, max_ticks)


def delay_ticks(game: Any, min_ticks: int, max_ticks: int | None = None, *,
                ctx: "ScriptContext | None" = None) -> bool:
    """Wait a random ``min_ticks..max_ticks`` server ticks (exactly ``min_ticks`` when
    ``max_ticks`` is None). True once they passed; False on a stop or a stalled clock."""
    ticks = random_between(min_ticks, min_ticks if max_ticks is None else max_ticks)
    return ConditionWait.for_context(ctx).ticks(_server_tick_of(game), ticks)


# --- Stopwatch / Timer / durations ---------------------------------------------------


class Stopwatch:
    """A pausable elapsed-time counter. Made stopped; :meth:`started` makes a running one."""

    def __init__(self, nano_clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._now = nano_clock
        self._accumulated = 0
        self._since = 0
        self._running = False

    @classmethod
    def started(cls, nano_clock: Callable[[], int] = time.monotonic_ns) -> "Stopwatch":
        return cls(nano_clock).start()

    def start(self) -> "Stopwatch":
        if not self._running:
            self._since = self._now()
            self._running = True
        return self

    resume = start

    def pause(self) -> "Stopwatch":
        if self._running:
            self._accumulated += self._now() - self._since
            self._running = False
        return self

    def reset(self) -> "Stopwatch":
        """Zero the count; a running stopwatch keeps running from now."""
        self._accumulated = 0
        self._since = self._now()
        return self

    @property
    def is_running(self) -> bool:
        return self._running

    def elapsed_ns(self) -> int:
        running = self._now() - self._since if self._running else 0
        return self._accumulated + running

    def elapsed_ms(self) -> int:
        return self.elapsed_ns() // _NS_PER_MS

    def elapsed_s(self) -> float:
        return self.elapsed_ns() / 1e9


class Timer:
    """A countdown of a random ``min_ms..max_ms`` length (or exactly ``min_ms``).
    :meth:`reset` restarts it with a new draw."""

    def __init__(self, min_ms: int, max_ms: int | None = None, *,
                 nano_clock: Callable[[], int] = time.monotonic_ns,
                 rng: _random.Random | None = None) -> None:
        max_ms = min_ms if max_ms is None else max_ms
        if min_ms < 0 or min_ms > max_ms:
            raise ValueError(f"need 0 <= min_ms <= max_ms, got {min_ms}..{max_ms}")
        self._min = min_ms
        self._max = max_ms
        self._now = nano_clock
        self._rng = rng
        self._duration = 0
        self._start = 0
        self.reset()

    def reset(self) -> "Timer":
        self._duration = random_between(self._min, self._max, self._rng)
        self._start = self._now()
        return self

    @property
    def duration_ms(self) -> int:
        return self._duration

    def remaining_ms(self) -> int:
        elapsed_ms = (self._now() - self._start) // _NS_PER_MS
        return max(0, self._duration - elapsed_ms)

    def has_expired(self) -> bool:
        return self.remaining_ms() == 0


def format_duration(ms: int | float) -> str:
    """``HH:MM:SS``, or ``Nd HH:MM:SS`` past a day. Negative reads as zero."""
    total_seconds = max(0, int(ms) // 1000)
    seconds = total_seconds % _SECONDS_PER_MINUTE
    total_minutes = total_seconds // _SECONDS_PER_MINUTE
    minutes = total_minutes % _MINUTES_PER_HOUR
    total_hours = total_minutes // _MINUTES_PER_HOUR
    hours = total_hours % _HOURS_PER_DAY
    days = total_hours // _HOURS_PER_DAY
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return clock if days == 0 else f"{days}d {clock}"


def per_hour(count: int | float, elapsed_ms: int | float) -> int:
    """``count`` scaled to one hour of ``elapsed_ms``; 0 when no time has passed."""
    if elapsed_ms <= 0:
        return 0
    return int(count * _MS_PER_HOUR / elapsed_ms)
