"""Blocking client-management waits end on a GUI stop in a worker (and behave exactly as
before under ``bwu_host --py``), and the CLI runner checks the manifest too."""
from __future__ import annotations

import threading
import time

import pytest

from botwithus.api import BotScript, ConfigSchemaError, script_manifest
from botwithus.api.clients import LaunchHandle, Subscription
from botwithus.core import ctl
from botwithus.core import native as native_mod
from botwithus.core.connection import Connection

from fakes import FakeNative

STOP_LATENCY_S = 0.25
STOP_AFTER_S = 0.1
LONG_WAIT_S = 5.0
# Ends a wait that ignored the stop, so a regression fails the test instead of hanging it.
BACKSTOP_S = 2.0


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    native_mod.set_backend(None)


def _worker():
    fake = FakeNative(ctl_mode=FakeNative.CTL_WORKER)
    native_mod.set_backend(fake)
    return fake


def _stop_later(fake):
    stamp = []

    def go():
        time.sleep(STOP_AFTER_S)
        stamp.append(time.monotonic())
        fake.gui_stop()
    threading.Thread(target=go, daemon=True).start()
    return stamp


def test_launch_outcome_raises_stop_requested_on_a_gui_stop():
    fake = _worker()
    stamp = _stop_later(fake)
    with pytest.raises(ctl.StopRequested):
        LaunchHandle("c1", 1).outcome(timeout_s=LONG_WAIT_S)
    assert time.monotonic() - stamp[0] < STOP_LATENCY_S


def test_launch_outcome_with_no_timeout_also_ends_on_stop():
    fake = _worker()
    _stop_later(fake)
    handle = LaunchHandle("c1", 1)
    backstop = threading.Timer(BACKSTOP_S, lambda: handle._complete("not stopped"))
    backstop.start()
    try:
        with pytest.raises(ctl.StopRequested):
            handle.outcome()
    finally:
        backstop.cancel()


def test_launch_outcome_still_times_out_and_completes_in_a_worker():
    _worker()
    with pytest.raises(TimeoutError):
        LaunchHandle("c1", 1).outcome(timeout_s=0.05)
    handle = LaunchHandle("c2", 2)
    threading.Timer(0.05, lambda: handle._complete("done")).start()
    assert handle.outcome(timeout_s=LONG_WAIT_S) == "done"


def test_launch_outcome_in_cli_mode_is_unchanged():
    native_mod.set_backend(FakeNative())
    with pytest.raises(TimeoutError):
        LaunchHandle("c1", 1).outcome(timeout_s=0.05)


def test_subscription_poll_returns_none_on_a_gui_stop():
    fake = _worker()
    stamp = _stop_later(fake)
    sub = Subscription(lambda s: None)
    assert sub.poll(timeout_s=LONG_WAIT_S) is None
    assert time.monotonic() - stamp[0] < STOP_LATENCY_S


def test_subscription_iteration_ends_on_a_gui_stop():
    fake = _worker()
    _stop_later(fake)
    sub = Subscription(lambda s: None)
    backstop = threading.Timer(BACKSTOP_S, sub.close)
    backstop.start()
    started = time.monotonic()
    try:
        assert list(sub) == []
        assert time.monotonic() - started < BACKSTOP_S / 2
    finally:
        backstop.cancel()


def test_subscription_poll_still_delivers_events_in_a_worker():
    _worker()
    sub = Subscription(lambda s: None)
    threading.Timer(0.05, lambda: sub._put("evt")).start()
    assert sub.poll(timeout_s=LONG_WAIT_S) == "evt"


def test_wait_stop_aware_returns_none_when_already_stopping():
    fake = _worker()
    fake.gui_stop()
    calls = []
    assert ctl.wait_stop_aware(lambda t: calls.append(t) or True, LONG_WAIT_S) is None
    assert calls == []


# --- the CLI runner checks the manifest (N3) --------------------------------------------------

class Bad(BotScript):
    def on_loop(self):
        return -1


def test_the_cli_runner_refuses_an_invalid_manifest_through_the_host():
    fake = FakeNative()
    native_mod.set_backend(fake)
    conn = Connection(4242)
    script = script_manifest(name="")(Bad)()
    with pytest.raises(ConfigSchemaError, match=r"^manifest\.name:"):
        conn.start_script(script)
    assert [e[0] for e in fake.ctl_log] == ["describe"]   # the host's check ran
    conn.close()


def test_a_second_cli_runner_falls_back_to_the_framework_check():
    fake = FakeNative()
    native_mod.set_backend(fake)
    conn = Connection(4242)

    @script_manifest(name="Good")
    class Good(BotScript):
        def on_loop(self):
            return -1
    conn.start_script(Good())                   # uses the host's one ctl_describe
    bad = script_manifest(name="x" * 129)(type("Bad2", (Bad,), {}))()
    with pytest.raises(ConfigSchemaError, match=r"^manifest\.name:"):
        conn.start_script(bad)
    assert [e[0] for e in fake.ctl_log] == ["describe"]
    conn.close()
