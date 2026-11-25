"""``botwithus.worker`` after review round 1 of #28: a stop during start,
BaseException from callbacks, load-time shape errors, the entry
run, the client exiting, an infinite delay, and an abstract plugin."""
from __future__ import annotations

import sys
import threading
import time

import pytest

from botwithus import worker
from botwithus.core import client_exit
from botwithus.core import native as native_mod

from fakes import FakeNative
from test_worker import (  # noqa: F401 - env is a fixture
    LONG_SLEEP_MS, PID, PLUGIN, SLEEPER, STOP_LATENCY_S, Recorder, _host, _names, _script, env)

STARTER = """
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        def on_start(self, ctx):
            T.events.append(("on_start",))
            T.during_start()
        def on_loop(self):
            T.events.append(("on_loop",))
            return 0
        def on_stop(self):
            T.events.append(("on_stop",))
"""


def _install(env, fake):
    env.trace.fake = fake
    native_mod.set_backend(fake)
    return fake


# --- a stop during start ---------------------------------------------------------------------

def test_a_stop_during_on_start_still_runs_on_stop_and_detaches(env):
    """The host has sent stopping, so ctl_state("running") is a no-op; the loop sees the
    stop, and the run ends stopped, not crashed."""
    fake = _host(env)
    env.trace.during_start = fake.gui_stop
    worker.run(_script(env, STARTER), PID)
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]
    assert ("on_loop",) not in env.trace.events
    assert fake.crashes == []


def test_a_host_that_refuses_running_after_its_stop_is_tolerated(env):
    """A host from before the no-op rule raises CtlError; with ctl_stopping() true the run
    still goes to on_stop and detach."""
    class StrictHost(Recorder):
        def ctl_state(self, phase):
            if self._ctl_host_stopped:
                self.trace.append(("state", phase))
                raise FakeNative.CtlError(f"ctl_state: illegal transition stopping -> {phase}")
            return super().ctl_state(phase)
    fake = _install(env, StrictHost(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    env.trace.during_start = fake.gui_stop
    worker.run(_script(env, STARTER), PID)
    assert _names(env.trace.events)[-2:] == ["on_stop", "detach"]
    assert fake.crashes == []


def test_a_ctl_state_refusal_without_a_stop_escapes_as_a_framework_bug(env):
    class Broken(Recorder):
        def ctl_state(self, phase):
            raise FakeNative.CtlError("ctl_state: illegal transition")
    _install(env, Broken(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    with pytest.raises(RuntimeError, match="illegal transition"):
        worker.run(_script(env, PLUGIN), PID)


# --- BaseException from callbacks ------------------------------------------------------------

@pytest.mark.parametrize("callback, raise_stmt, phase", [
    ("on_start", "raise SystemExit(3)", "on_start"),
    ("on_start", "raise KeyboardInterrupt()", "on_start"),
    ("on_loop", "raise KeyboardInterrupt()", "on_loop"),
    ("on_loop", "raise SystemExit(2)", "on_loop"),
    ("on_loop", "raise SystemExit('bad')", "on_loop"),
    ("on_loop", "raise SystemExit(True)", "on_loop"),
    ("on_config_update", "raise SystemExit(0)", "on_config_update"),
    ("on_stop", "raise SystemExit(1)", "on_stop"),
])
def test_base_exceptions_from_callbacks_are_that_phases_crash(env, callback, raise_stmt, phase):
    fake = _host(env)
    body = {name: "pass" for name in ("on_start", "on_config_update", "on_stop")}
    body["on_loop"] = "return -1"
    body[callback] = raise_stmt
    worker.run(_script(env, f"""
        from botwithus.api import BotScript
        class S(BotScript):
            def on_start(self, ctx): {body['on_start']}
            def on_config_update(self, config): {body['on_config_update']}
            def on_loop(self): {body['on_loop']}
            def on_stop(self): {body['on_stop']}
    """), PID)
    assert [c["phase"] for c in fake.crashes] == [phase]
    assert _names(env.trace.events)[-1] == "detach"


@pytest.mark.parametrize("code", [None, 0])
def test_a_clean_system_exit_in_on_loop_ends_like_a_negative_delay(env, code):
    fake = _host(env)
    worker.run(_script(env, f"""
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_loop(self): raise SystemExit({code!r})
            def on_stop(self): T.events.append(("on_stop",))
    """), PID)
    assert fake.crashes == []
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]


# --- load-time shape errors ------------------------------------------------------------------

@pytest.mark.parametrize("fields, message", [
    ("'not a list'", "ConfigSchemaError: config_schema: expected a list of fields"),
    ("[42]", "ConfigSchemaError: config_schema[0]: expected a ConfigField or a dict, got int"),
    ("{'key': 'k'}", "ConfigSchemaError: config_schema: expected a list of fields"),
])
def test_get_config_fields_of_the_wrong_shape_is_a_load_crash(env, fields, message):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, f"""
        from botwithus.api import BotScript
        class S(BotScript):
            def get_config_fields(self): return {fields}
            def on_loop(self): return -1
    """))
    assert fake.ctl_log == []
    assert [(c["phase"], c["exception"]) for c in fake.crashes] == [("load", message)]


def test_a_wrongly_typed_dict_field_is_refused_by_the_host_with_its_key_path(env):
    """The binding reads desc by key name and refuses a wrong type with its key path (F1);
    the framework then returns quietly and the host reports crash{schema}."""
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    seen = []
    original = fake.ctl_describe

    def spy(desc):
        try:
            return original(desc)
        except FakeNative.CtlError as exc:
            seen.append(str(exc))
            raise
    fake.ctl_describe = spy
    worker.describe(_script(env, """
        from botwithus.api import BotScript
        class S(BotScript):
            def get_config_fields(self):
                return [{"key": "n", "kind": "int", "label": "N", "default": "five"}]
            def on_loop(self): return -1
    """))
    assert seen == ["config_schema[0].default: expected an integer"]
    assert fake.crashes == []


def test_an_abstract_botscript_defined_in_the_file_is_a_load_crash(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, """
        from botwithus.api import BotScript
        class Forgot(BotScript):
            def on_start(self, ctx): pass
    """))
    assert fake.ctl_log == []
    assert [(c["phase"], c["exception"]) for c in fake.crashes] == [
        ("load", "Forgot: on_loop is missing")]


# --- the entry run ---------------------------------------------------------------------------

def test_a_self_run_crash_goes_to_the_run_opened_at_entry(env):
    """The crash block goes to the entry run (the adopted host run), even after the script's
    own runner opened another run."""
    fake = _host(env)
    _script(env, """
        from botwithus.api import BotScript
        class Inner(BotScript):
            def on_loop(self): return -1
    """, name="inner_for_entry_run.py")
    worker.run(_script(env, """
        if __name__ == "__main__":
            import time
            from inner_for_entry_run import Inner
            from botwithus.core.connection import Connection
            conn = Connection(4242)
            conn.start_script(Inner())
            while conn.running_scripts():
                time.sleep(0.01)
            conn.close()
            raise RuntimeError("after the runner")
    """), PID)
    sys.modules.pop("inner_for_entry_run", None)
    assert len(fake.runlog_opens) == 2               # the entry run, then the runner's
    assert [(c["run"], c["phase"]) for c in fake.crashes] == [(1, "on_loop")]


def test_a_load_crash_goes_to_the_entry_run(env):
    fake = _host(env)
    worker.run(_script(env, "raise ValueError('x')\n"), PID)
    assert [o["name"] for o in fake.runlog_opens] == ["script"]
    assert [c["run"] for c in fake.crashes] == [1]
    assert fake.runlog_closed == [1]


# --- the client exiting ----------------------------------------------------------------------

CONFIRM_S = 0.1   # CLIENT_GONE_CONFIRM_S, shortened so the suite stays fast


@pytest.fixture
def quick_confirm(monkeypatch):
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", CONFIRM_S)


class ExitingClient(Recorder):
    """The native host after its client exits: ``refresh`` turns False and the pid drops out
    of discovery, while the reads keep returning the last frame (they never raise)."""

    exited = False
    listed_misses = None   # None: unlisted for good once exited; n: the next n looks miss

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.discoveries = []   # (monotonic time, listed?) per discover_pids call

    def refresh(self, handle):
        return not self.exited

    def discover_pids(self):
        if not self.exited:
            listed = True
        elif self.listed_misses is None:
            listed = False
        else:
            listed = self.listed_misses <= 0
            self.listed_misses -= 1
        self.discoveries.append((time.monotonic(), listed))
        return super().discover_pids() if listed else []


EXITS_ONCE = """
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        n = 0
        def on_start(self, ctx): S.ctx = ctx
        def on_loop(self):
            S.n += 1
            T.events.append(("on_loop",))
            T.fake.exited = True
            return -1 if S.n >= 3 else 0     # a backstop if the exit went unseen
        def on_stop(self):
            T.events.append(("on_stop", S.ctx.game.snapshot().server_tick))
"""


def test_the_client_exiting_ends_the_loop_cleanly(env, quick_confirm):
    """refresh False and the pid unlisted on two looks -> stopping, on_stop, detach, no
    crash. on_stop still reads the last frame (the fake's server_tick is 100)."""
    fake = _install(env, ExitingClient(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    worker.run(_script(env, EXITS_ONCE), PID)
    assert env.trace.events.count(("on_loop",)) == 1
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]
    assert ("on_stop", 100) in env.trace.events
    assert fake.crashes == []


def test_the_exit_needs_two_misses_at_least_the_confirm_time_apart(env, quick_confirm):
    fake = _install(env, ExitingClient(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    worker.run(_script(env, EXITS_ONCE), PID)
    misses = [t for t, listed in fake.discoveries if not listed]
    assert len(misses) == 2
    assert misses[1] - misses[0] >= CONFIRM_S


def test_a_refresh_failure_with_the_client_still_listed_keeps_running(env, quick_confirm):
    class StillListed(ExitingClient):
        def discover_pids(self):
            return Recorder.discover_pids(self)
    _install(env, StillListed(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    worker.run(_script(env, EXITS_ONCE), PID)
    assert env.trace.events.count(("on_loop",)) == 3   # ran to the backstop


def test_an_unlisted_pid_that_still_refreshes_keeps_running(env, quick_confirm):
    class StillRefreshing(ExitingClient):
        def refresh(self, handle):
            return True
    _install(env, StillRefreshing(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    worker.run(_script(env, EXITS_ONCE), PID)
    assert env.trace.events.count(("on_loop",)) == 3


def test_a_single_discovery_miss_keeps_running(env, quick_confirm):
    fake = ExitingClient(env.trace.events, ctl_mode=FakeNative.CTL_WORKER)
    fake.listed_misses = 1     # one flap, then listed again
    _install(env, fake)
    worker.run(_script(env, EXITS_ONCE.replace(
        "T.fake.exited = True", "T.fake.exited = True; T.fake.listed_misses = 1")), PID)
    assert env.trace.events.count(("on_loop",)) == 3


def test_a_stop_during_the_confirm_wait_ends_the_run_stopped(env, monkeypatch):
    """The wait between the two looks is stop-aware: a GUI stop ends it at once, and the run
    ends through the normal stop path."""
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", 30.0)
    fake = _install(env, ExitingClient(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    timer = threading.Timer(0.2, fake.gui_stop)
    timer.start()
    started = time.monotonic()
    worker.run(_script(env, EXITS_ONCE), PID)
    timer.join()
    assert time.monotonic() - started < STOP_LATENCY_S + 0.2
    assert _names(env.trace.events)[-2:] == ["on_stop", "detach"]
    assert fake.crashes == []


def test_the_confirm_time_defaults_to_one_second():
    assert client_exit.CLIENT_GONE_CONFIRM_S == 1.0


# --- an infinite delay -----------------------------------------------------------------------

def test_an_infinite_delay_sleeps_until_the_stop(env):
    """Lua parity: inf is a number, so the loop sleeps (stop-aware) rather than ending."""
    fake = _host(env)
    env.trace.loop_entered = threading.Event()
    stopped_at = []

    def gui():
        env.trace.loop_entered.wait(5)
        time.sleep(0.2)
        stopped_at.append(time.monotonic())
        fake.gui_stop()
    t = threading.Thread(target=gui)
    t.start()
    source = SLEEPER.replace("return %d" % LONG_SLEEP_MS, "return float('inf')")
    assert "float('inf')" in source
    worker.run(_script(env, source), PID)
    t.join()
    on_stop = next(e for e in env.trace.events if e[0] == "on_stop")
    assert on_stop[1] - stopped_at[0] < STOP_LATENCY_S
    assert env.trace.events.count(("on_loop",)) == 1
    assert fake.crashes == []
