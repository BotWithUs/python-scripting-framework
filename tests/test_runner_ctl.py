"""``ScriptRunner`` and the ctl seam outside ``botwithus.worker``: the CLI-mode runner (config
parity with the worker, the stop state), a runner inside a GUI worker (the GUI's stop wakes
its sleep), the seam's CLI fallback, and the attach target's precedence (section 1, N4)."""
from __future__ import annotations

import threading
import time

import pytest

from botwithus.api import BotScript, ConfigSchemaError, int_field
from botwithus.core import ctl
from botwithus.core import native as native_mod
from botwithus.core.connection import Connection
from botwithus.core.runtime import ScriptState

from fakes import FakeNative

STOP_LATENCY_S = 0.25
JOIN_S = 5.0


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    native_mod.set_backend(None)


def _wait_finished(runner):
    runner._thread.join(JOIN_S)
    assert not runner._thread.is_alive()


class Quick(BotScript):
    """Records its callbacks; stops itself after one loop."""

    def __init__(self, fields=None, loop_delay=-1):
        self.calls = []
        self._fields = fields or []
        self._delay = loop_delay
        self.loop_entered = threading.Event()

    def get_config_fields(self):
        return self._fields

    def on_config_update(self, config):
        self.calls.append(("config", dict(config)))

    def on_start(self, ctx):
        self.ctx = ctx
        self.calls.append(("on_start", dict(ctx.config)))

    def on_loop(self):
        self.calls.append(("on_loop",))
        self.loop_entered.set()
        return self._delay

    def on_stop(self):
        self.calls.append(("on_stop", time.monotonic()))


# --- the stop state (pre-existing bug) ------------------------------------------------------

def test_stop_on_a_runner_that_already_finished_leaves_it_stopped():
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    conn.start_script(Quick())
    runner = conn._runners["Quick"]
    _wait_finished(runner)
    assert runner.state is ScriptState.STOPPED
    runner.stop()
    assert runner.state is ScriptState.STOPPED     # was left at STOPPING
    conn.close()
    assert runner.state is ScriptState.STOPPED


def test_stop_on_a_crashed_runner_keeps_error():
    class Crashes(Quick):
        def on_loop(self):
            raise ValueError("boom")
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    conn.start_script(Crashes())
    runner = conn._runners["Crashes"]
    _wait_finished(runner)
    runner.stop()
    assert runner.state is ScriptState.ERROR
    conn.close()


def test_stop_on_a_running_runner_waits_and_ends_stopped():
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    script = Quick(loop_delay=10_000)
    conn.start_script(script)
    script.loop_entered.wait(JOIN_S)
    runner = conn._runners["Quick"]
    runner.stop()
    assert runner.state is ScriptState.STOPPED
    assert script.calls[-1][0] == "on_stop"
    conn.close()


def test_stop_on_a_never_started_runner_is_stopped():
    from botwithus.core.runtime import ScriptRunner
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    runner = ScriptRunner(Quick(), conn.new_context("q", threading.Event()))
    runner.stop()
    assert runner.state is ScriptState.STOPPED
    conn.close()


# --- CLI parity ------------------------------------------------------------------------------

def test_the_cli_runner_configures_with_the_defaults_before_on_start():
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    script = Quick(fields=[int_field("n", "N", 4)])
    conn.start_script(script)
    _wait_finished(conn._runners["Quick"])
    assert script.calls[:3] == [("config", {"n": 4}), ("on_start", {"n": 4}), ("on_loop",)]
    conn.close()


def test_an_invalid_schema_refuses_to_start_and_registers_nothing():
    native_mod.set_backend(FakeNative())
    conn = Connection(4242)
    script = Quick(fields=[int_field("n", "N", 50, max=10)])
    with pytest.raises(ConfigSchemaError, match=r"^config_schema\[0\]\.default:"):
        conn.start_script(script)
    assert script.calls == [] and conn._runners == {}
    conn.close()


def test_the_cli_runner_starts_no_stop_watcher():
    fake = FakeNative()
    native_mod.set_backend(fake)
    conn = Connection(4242)
    conn.start_script(Quick())
    _wait_finished(conn._runners["Quick"])
    assert fake.ctl_wait_calls == 0
    conn.close()


# --- a runner inside a GUI worker -------------------------------------------------------------

def test_the_gui_stop_wakes_a_runner_sleep_within_250_ms():
    fake = FakeNative(ctl_mode=FakeNative.CTL_WORKER)
    native_mod.set_backend(fake)
    conn = Connection(4242)
    script = Quick(loop_delay=10_000)
    conn.start_script(script)
    assert script.loop_entered.wait(JOIN_S)
    time.sleep(0.05)
    stopped_at = time.monotonic()
    fake.gui_stop()
    runner = conn._runners["Quick"]
    _wait_finished(runner)
    assert script.calls[-1][1] - stopped_at < STOP_LATENCY_S
    assert script.ctx.stopping is True
    assert runner.state is ScriptState.STOPPED
    assert script.calls.count(("on_loop",)) == 1
    conn.close()


def test_the_stop_watcher_ends_with_its_run():
    fake = FakeNative(ctl_mode=FakeNative.CTL_WORKER)
    native_mod.set_backend(fake)
    conn = Connection(4242)
    conn.start_script(Quick())
    _wait_finished(conn._runners["Quick"])
    assert not any(t.name == "bwu-ctl-stop-watch" for t in threading.enumerate())
    conn.close()


# --- the seam --------------------------------------------------------------------------------

class NoCtl:
    """A backend from before the ctl surface."""


def test_without_the_surface_the_seam_behaves_as_cli():
    native_mod.set_backend(NoCtl())
    assert ctl.mode() == ctl.MODE_CLI
    assert ctl.stopping() is False
    assert ctl.take_config() is None
    assert ctl.wait(1) == ctl.WAKE_TIMEOUT
    ctl.state("running")
    ctl.config_done(1, True)
    desc = {"manifest": {"name": "n", "version": "1", "author": "", "description": ""},
            "config_schema": [int_field("n", "N", 2).to_wire()]}
    assert ctl.describe(desc) == {"n": 2}
    bad = dict(desc, config_schema=[int_field("n", "N", 20, max=3).to_wire()])
    with pytest.raises(ctl.CtlError, match=r"^config_schema\[0\]\.default:"):
        ctl.describe(bad)


def test_the_seam_converts_the_native_ctl_error():
    native_mod.set_backend(FakeNative(ctl_mode=FakeNative.CTL_WORKER,
                                      ctl_describe_error="manifest.name: invalid manifest"))
    with pytest.raises(ctl.CtlError, match="manifest.name: invalid manifest"):
        ctl.describe({"manifest": {}, "config_schema": []})


def test_the_seam_clamps_wait_to_the_native_range():
    fake = FakeNative(ctl_mode=FakeNative.CTL_WORKER)
    native_mod.set_backend(fake)
    fake.gui_stop()
    assert ctl.wait(-5) == ctl.WAKE_STOP
    assert ctl.wait(2 ** 40) == ctl.WAKE_STOP


def test_an_illegal_phase_raises_ctl_error():
    native_mod.set_backend(FakeNative(ctl_mode=FakeNative.CTL_WORKER))
    with pytest.raises(ctl.CtlError):
        ctl.state("running")      # running before attaching, in a plugin run


# --- the attach target (section 1, N4) --------------------------------------------------------

@pytest.mark.parametrize("mode, env, explicit, expected", [
    (FakeNative.CTL_WORKER, "777", None, 777),       # env in worker mode
    (FakeNative.CTL_WORKER, "777", 55, 55),          # explicit wins
    (FakeNative.CTL_CLI, "777", None, 4242),         # env ignored outside a worker
    (FakeNative.CTL_WORKER, "", None, 4242),         # no env: discovery
    (FakeNative.CTL_WORKER, "junk", None, 4242),
])
def test_default_client_pid_precedence(monkeypatch, mode, env, explicit, expected):
    native_mod.set_backend(FakeNative(ctl_mode=mode))
    monkeypatch.setenv("BWU_CLIENT_PID", env)
    assert native_mod.default_client_pid(explicit) == expected


def test_default_client_pid_is_none_without_a_client(monkeypatch):
    class NoClients(FakeNative):
        def discover_pids(self):
            return []
    native_mod.set_backend(NoClients())
    monkeypatch.delenv("BWU_CLIENT_PID", raising=False)
    assert native_mod.default_client_pid() is None
