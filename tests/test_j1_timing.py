"""J1 condition waits (with J3 pause), Stopwatch, Timer and the duration helpers.

The wait engine runs on an injected clock and sleeper, so these tests are exact and instant.
"""
import random
import threading
import time

import pytest

from botwithus.api import timing
from botwithus.api.timing import (ConditionWait, Stopwatch, Timer, bound_context,
                                  current_context, format_duration, per_hour)

NS = 1_000_000


class Clock:
    """A fake monotonic clock: sleeping advances it, and hooks run at given times."""

    def __init__(self):
        self.now = 0
        self.sleeps = []
        self.hooks = []  # (at_ns, fn)

    def __call__(self):
        return self.now

    def sleep(self, ms):
        self.sleeps.append(ms)
        self.now += ms * NS
        for at, fn in list(self.hooks):
            if self.now >= at:
                self.hooks.remove((at, fn))
                fn()


def engine(clock, stop=lambda: False, paused=lambda: False):
    return ConditionWait(clock, clock.sleep, stop, paused)


def test_until_returns_true_when_the_condition_becomes_true():
    # Failure: False, or a wait that kept polling after the condition was met.
    clock = Clock()
    hits = iter([False, False, True])
    assert engine(clock).until(lambda: next(hits), 5_000) is True
    assert clock.sleeps == [50, 50]


def test_until_gives_up_at_its_timeout_and_never_sleeps_past_it():
    # Failure: running past 130 ms, or a last sleep longer than what was left.
    clock = Clock()
    assert engine(clock).until(lambda: False, 130) is False
    assert sum(clock.sleeps) == 130 and clock.sleeps[-1] == 30


def test_until_leaves_within_one_poll_of_a_stop():
    # Failure: a wait that keeps polling after the stop signal (runs to its 60 s timeout).
    clock = Clock()
    stopped = []
    clock.hooks.append((200 * NS, lambda: stopped.append(1)))
    assert engine(clock, stop=lambda: bool(stopped)).until(lambda: False, 60_000) is False
    assert clock.now <= 250 * NS


def test_a_stop_before_the_wait_returns_false_without_testing_the_condition():
    tested = []
    assert engine(Clock(), stop=lambda: True).until(lambda: tested.append(1) or True, 1000) \
        is False
    assert tested == []


def test_a_pause_holds_the_wait_without_spending_its_timeout():
    # Failure: the condition tested while paused, or the paused time eaten from the 300 ms.
    clock = Clock()
    paused = [True]
    tested_at = []
    clock.hooks.append((1_000 * NS, lambda: paused.clear()))

    def cond():
        tested_at.append(clock.now)
        return False

    assert engine(clock, paused=lambda: bool(paused)).until(cond, 300) is False
    assert tested_at[0] >= 1_000 * NS          # nothing tested while paused
    assert clock.now >= 1_000 * NS + 300 * NS  # the full 300 ms ran after the resume


def test_ticks_waits_for_the_server_tick_to_advance():
    # Failure: counting polls or wall time instead of server ticks.
    clock = Clock()
    tick = [500]
    clock.hooks.append((300 * NS, lambda: tick.__setitem__(0, 501)))
    clock.hooks.append((900 * NS, lambda: tick.__setitem__(0, 502)))
    assert engine(clock).ticks(lambda: tick[0], 2) is True
    assert 900 * NS <= clock.now < 1_000 * NS


def test_until_ticks_gives_up_after_max_ticks():
    clock = Clock()
    tick = [10]

    def advance():
        tick[0] += 1
        clock.hooks.append((clock.now + 600 * NS, advance))
    clock.hooks.append((600 * NS, advance))
    assert engine(clock).until_ticks(lambda: tick[0], lambda: False, 3) is False
    assert tick[0] == 13


def test_a_stalled_tick_cannot_hang_the_wait():
    # Failure: waiting forever with no tick (logged out): it must give up at 2 tick lengths per
    # tick of wall time.
    clock = Clock()
    assert engine(clock).ticks(lambda: None, 2) is False
    assert clock.now == pytest.approx((2 + 1) * 600 * 2 * NS, abs=60 * NS)


def test_ticks_that_pass_while_paused_do_not_count():
    # Failure: a delay_ticks(5) that returns on resume because 5 ticks passed while paused.
    clock = Clock()
    tick = [100]
    paused = [True]

    def advance():
        tick[0] += 1
        clock.hooks.append((clock.now + 600 * NS, advance))
    clock.hooks.append((600 * NS, advance))
    clock.hooks.append((6_000 * NS, lambda: paused.clear()))
    assert engine(clock, paused=lambda: bool(paused)).ticks(lambda: tick[0], 5) is True
    assert clock.now >= 6_000 * NS + 4 * 600 * NS


class _Ctx:
    def __init__(self):
        self.stop = threading.Event()
        self.is_paused = False

    def is_stop_requested(self):
        return self.stop.is_set()


def test_module_waits_find_the_bound_context_stop():
    # Failure: delay_until ignoring the script's stop when called with no ctx argument.
    ctx = _Ctx()
    threading.Timer(0.1, ctx.stop.set).start()
    start = time.monotonic()
    with bound_context(ctx):
        assert current_context() is ctx
        assert timing.delay_until(lambda: False, 10_000) is False
    assert time.monotonic() - start < 1.0
    assert current_context() is None


def test_stopwatch_counts_only_running_time():
    # Failure: paused time counted, or reset stopping a running watch.
    clock = Clock()
    watch = Stopwatch.started(clock)
    clock.sleep(1500)
    watch.pause()
    clock.sleep(4000)
    assert watch.elapsed_ms() == 1500
    watch.resume()
    clock.sleep(250)
    assert watch.elapsed_ms() == 1750
    watch.reset()
    clock.sleep(10)
    assert watch.elapsed_ms() == 10 and watch.is_running


def test_timer_expires_after_its_drawn_duration_and_reset_redraws():
    clock = Clock()
    timer = Timer(400, 900, nano_clock=clock, rng=random.Random(7))
    first = timer.duration_ms
    assert 400 <= first <= 900
    clock.sleep(first - 1)
    assert not timer.has_expired() and timer.remaining_ms() == 1
    clock.sleep(1)
    assert timer.has_expired()
    timer.reset()
    assert timer.remaining_ms() == timer.duration_ms
    with pytest.raises(ValueError):
        Timer(500, 100)


def test_format_duration_and_per_hour():
    # Failure: days not split out, or per-hour mis-scaled (perHour 1800 in 30 min = 3600).
    assert format_duration(3_723_000) == "01:02:03"
    assert format_duration(90_061_000) == "1d 01:01:01"
    assert format_duration(-5) == "00:00:00"
    assert per_hour(1800, 30 * 60 * 1000) == 3600
    assert per_hour(5, 0) == 0
