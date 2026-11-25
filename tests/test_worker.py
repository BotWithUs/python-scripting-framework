"""``botwithus.worker``: what the native host calls to describe or run a script for the GUI
(``docs/GUI-WORKER-PROTOCOL.md`` sections 7.2, 9.3, 9.4, 9.6 and 9.7), against the fake ctl
surface. Each test writes a real script file and drives it the way ``bwu_host`` would."""
from __future__ import annotations

import sys
import textwrap
import threading
import time
import types

import pytest

from botwithus import worker
from botwithus.core import native as native_mod

from fakes import FakeNative, FakeRunlog

# The protocol's stop latency bound (section 9.7).
STOP_LATENCY_S = 0.25
# A delay much longer than the test: only a stop can end the sleep.
LONG_SLEEP_MS = 5000
PID = 4242


class Recorder(FakeRunlog):
    """The fake host, also writing its ctl calls into the shared trace so a test can check
    the order of script callbacks against host calls."""

    def __init__(self, trace: list, **kw) -> None:
        super().__init__(**kw)
        self.trace = trace
        self.attached = 0

    def ctl_state(self, phase):
        self.trace.append(("state", phase))
        return super().ctl_state(phase)

    def ctl_describe(self, desc):
        self.trace.append(("describe",))
        return super().ctl_describe(desc)

    def attach(self, pid):
        self.attached += 1
        self.trace.append(("attach", pid))
        return super().attach(pid)

    def detach(self, handle):
        self.trace.append(("detach",))
        super().detach(handle)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A trace module the scripts write into, a clean ``bwu_script`` slot and ``sys.path``."""
    trace_mod = types.ModuleType("worker_trace")
    trace_mod.events = []
    trace_mod.fake = None
    monkeypatch.setitem(sys.modules, "worker_trace", trace_mod)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield types.SimpleNamespace(tmp=tmp_path, trace=trace_mod)
    sys.modules.pop("bwu_script", None)
    native_mod.set_backend(None)


def _host(env, mode=FakeNative.CTL_WORKER, **kw) -> Recorder:
    fake = Recorder(env.trace.events, ctl_mode=mode, **kw)
    env.trace.fake = fake
    native_mod.set_backend(fake)
    return fake


def _script(env, body: str, name: str = "script.py") -> str:
    path = env.tmp / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


PLUGIN = """
    import worker_trace as T
    from botwithus.api import BotScript, script_manifest, int_field, choice_field

    T.events.append(("import", __name__))

    @script_manifest(name="Cutter", version="2.0", description="cuts", category="Woodcutting")
    class Cutter(BotScript):
        def __init__(self):
            self.loops = 0

        def get_config_fields(self):
            return [int_field("n", "Count", 3, min=1, max=10),
                    choice_field("tree", "Tree", ["Oak", "Yew"], default="Yew")]

        def on_config_update(self, config):
            T.events.append(("config", dict(config)))

        def on_start(self, ctx):
            self.ctx = ctx
            T.events.append(("on_start", dict(ctx.config)))

        def on_loop(self):
            self.loops += 1
            T.events.append(("on_loop", self.loops))
            return -1 if self.loops >= 2 else 0

        def on_stop(self):
            T.events.append(("on_stop",))
"""


def _names(trace) -> list:
    return [e[0] if e[0] != "state" else f"state:{e[1]}" for e in trace]


# --- describe --------------------------------------------------------------------------------

def test_describe_reports_the_plugin_without_attaching_or_calling_back(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, PLUGIN))
    desc = fake.ctl_log[0][1]
    assert desc == {
        "manifest": {"name": "Cutter", "version": "2.0", "author": "", "description": "cuts",
                     "category": "Woodcutting"},
        "config_schema": [
            {"key": "n", "kind": "int", "label": "Count", "default": 3, "min": 1, "max": 10},
            {"key": "tree", "kind": "choice", "label": "Tree", "default": "Yew",
             "options": ["Oak", "Yew"]},
        ],
    }
    assert fake.attached == 0
    assert _names(env.trace.events) == ["import", "describe"]
    assert env.trace.events[0] == ("import", "bwu_script")   # never __main__
    assert fake.crashes == []


def test_describe_of_a_self_running_file_uses_its_name_and_does_not_self_run(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, """
        import worker_trace as T
        if __name__ == "__main__":
            T.events.append(("self_run",))
    """, name="my_tool.py"))
    assert fake.ctl_log[0][1] == {
        "manifest": {"name": "my_tool", "version": "1.0", "author": "", "description": ""},
        "config_schema": [], "self_running": True}
    assert ("self_run",) not in env.trace.events


def test_the_script_directory_is_first_on_sys_path(env):
    _host(env, FakeNative.CTL_DESCRIBE)
    (env.tmp / "helper_mod_for_worker.py").write_text("VALUE = 7\n", encoding="utf-8")
    worker.describe(_script(env, """
        import worker_trace as T
        import helper_mod_for_worker
        T.events.append(("helper", helper_mod_for_worker.VALUE))
    """))
    assert ("helper", 7) in env.trace.events
    assert sys.path[0] == str(env.tmp)
    sys.modules.pop("helper_mod_for_worker", None)


def test_two_plugins_in_one_file_is_a_load_crash(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, """
        from botwithus.api import BotScript
        class Beta(BotScript):
            def on_loop(self): return -1
        class Alpha(BotScript):
            def on_loop(self): return -1
    """, name="two.py"))
    assert fake.ctl_log == []   # never described
    assert [(c["phase"], c["exception"]) for c in fake.crashes] == [
        ("load", "more than one BotScript in two.py: Alpha, Beta")]


def test_an_imported_or_abstract_botscript_is_not_the_plugin(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, """
        import abc
        from botwithus.api import BotScript, script_manifest
        from botwithus.api.script import BotScript as Again   # not defined here

        class Base(BotScript):          # abstract: on_loop not implemented
            pass

        @script_manifest(name="Real")
        class Real(Base):
            def on_loop(self): return -1
    """))
    assert fake.ctl_log[0][1]["manifest"]["name"] == "Real"


@pytest.mark.parametrize("body, needle", [
    ("def broken(:\n", "SyntaxError"),
    ("raise ValueError('boom at import')\n", "ValueError: boom at import"),
    ("import no_such_module_anywhere\n", "ModuleNotFoundError"),
])
def test_a_file_that_does_not_import_is_a_load_crash(env, body, needle):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, body))
    assert fake.ctl_log == []
    assert len(fake.crashes) == 1 and fake.crashes[0]["phase"] == "load"
    assert needle in fake.crashes[0]["exception"]


def test_a_failing_get_config_fields_is_a_load_crash(env):
    fake = _host(env, FakeNative.CTL_DESCRIBE)
    worker.describe(_script(env, """
        from botwithus.api import BotScript
        class S(BotScript):
            def get_config_fields(self): raise RuntimeError("no fields today")
            def on_loop(self): return -1
    """))
    assert fake.ctl_log == []
    assert fake.crashes[0]["phase"] == "load"
    assert "no fields today" in fake.crashes[0]["exception"]


def test_an_invalid_description_ends_quietly_for_the_host_to_report(env):
    fake = _host(env, FakeNative.CTL_WORKER,
                 ctl_describe_error="config_schema[0].default: invalid config field")
    worker.run(_script(env, PLUGIN), PID)
    assert _names(env.trace.events) == ["import", "describe"]   # no attach, no callbacks
    assert fake.crashes == []    # the host has the reason: crash{schema}


# --- a plugin run ----------------------------------------------------------------------------

def test_a_plugin_run_follows_the_normative_order(env):
    fake = _host(env, ctl_saved_config={"n": 7, "gone": 1})
    worker.run(_script(env, PLUGIN), PID)
    assert _names(env.trace.events) == [
        "import", "describe", "state:attaching", "attach", "config", "on_start",
        "state:running", "on_loop", "on_loop", "state:stopping", "on_stop", "detach"]
    initial = {"n": 7, "tree": "Yew"}
    assert ("config", initial) in env.trace.events
    assert ("on_start", initial) in env.trace.events     # configured before on_start
    assert fake.crashes == []
    # One run, opened at entry (the adopted host run), named after the file.
    assert [(o["name"], o["pid"]) for o in fake.runlog_opens] == [("script", PID)]
    assert fake.runlog_closed == [1]


def test_stop_before_attach_runs_nothing(env):
    fake = _host(env)
    fake.gui_stop()
    worker.run(_script(env, PLUGIN), PID)
    assert _names(env.trace.events) == ["import", "describe"]
    assert fake.attached == 0


def test_a_failed_attach_is_reported_as_phase_other(env):
    class NoClient(Recorder):
        def attach(self, pid):
            raise OSError(2, "no such client")
    fake = NoClient(env.trace.events, ctl_mode=FakeNative.CTL_WORKER)
    native_mod.set_backend(fake)
    worker.run(_script(env, PLUGIN), PID)
    assert _names(env.trace.events) == ["import", "describe", "state:attaching"]
    assert [(c["phase"], c["exception"]) for c in fake.crashes] == [
        ("other", "attach failed: 2: no such client")]


def test_a_crash_in_on_loop_still_runs_on_stop_and_detaches(env):
    fake = _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_loop(self):
                T.events.append(("on_loop",))
                raise ValueError("boom")
            def on_stop(self):
                T.events.append(("on_stop",))
    """), PID)
    assert _names(env.trace.events)[-5:] == [
        "state:running", "on_loop", "state:stopping", "on_stop", "detach"]
    assert [(c["phase"], c["iteration"]) for c in fake.crashes] == [("on_loop", 1)]
    assert "ValueError: boom" in fake.crashes[0]["exception"]


def test_a_crash_in_on_start_runs_on_stop_but_never_reports_running(env):
    fake = _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_start(self, ctx): raise RuntimeError("cannot start")
            def on_loop(self): T.events.append(("on_loop",)); return -1
            def on_stop(self): T.events.append(("on_stop",))
    """), PID)
    assert fake.ctl_states() == ["attaching"]
    assert _names(env.trace.events)[-2:] == ["on_stop", "detach"]
    assert ("on_loop",) not in env.trace.events
    assert fake.crashes[0]["phase"] == "on_start"


def test_a_crash_in_the_initial_config_update_skips_start_and_stop(env):
    fake = _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_config_update(self, config): raise KeyError("missing")
            def on_start(self, ctx): T.events.append(("on_start",))
            def on_loop(self): return -1
            def on_stop(self): T.events.append(("on_stop",))
    """), PID)
    assert ("on_start",) not in env.trace.events and ("on_stop",) not in env.trace.events
    assert fake.crashes[0]["phase"] == "on_config_update"
    assert fake.ctl_states() == ["attaching"]
    assert _names(env.trace.events)[-1] == "detach"


@pytest.mark.parametrize("delay", [None, "soon", True, float("nan"), -5, -float("inf")])
def test_a_delay_that_is_not_a_non_negative_number_ends_the_loop(env, delay):
    fake = _host(env)
    env.trace.delay = delay
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_loop(self): T.events.append(("on_loop",)); return T.delay
    """), PID)
    assert env.trace.events.count(("on_loop",)) == 1
    assert fake.ctl_states()[-1] == "stopping" and fake.crashes == []


# --- stop -------------------------------------------------------------------------------------

SLEEPER = """
    import time
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        def on_start(self, ctx): self.ctx = ctx
        def on_loop(self):
            T.events.append(("on_loop",))
            T.loop_entered.set()
            return %d
        def on_stop(self):
            T.events.append(("on_stop", time.monotonic(), self.ctx.stopping))
""" % LONG_SLEEP_MS


def test_stop_wakes_a_sleeping_plugin_within_250_ms(env):
    fake = _host(env)
    env.trace.loop_entered = threading.Event()
    stopped_at = []

    def gui():
        env.trace.loop_entered.wait(5)
        time.sleep(0.05)          # let the loop reach its sleep
        stopped_at.append(time.monotonic())
        fake.gui_stop()

    t = threading.Thread(target=gui)
    t.start()
    worker.run(_script(env, SLEEPER), PID)
    t.join()
    on_stop = next(e for e in env.trace.events if e[0] == "on_stop")
    assert on_stop[1] - stopped_at[0] < STOP_LATENCY_S
    assert on_stop[2] is True                       # ctx.stopping
    assert env.trace.events.count(("on_loop",)) == 1   # no on_loop after stop
    assert fake.ctl_states() == ["attaching", "running", "stopping"]


def test_ctx_sleep_returns_false_on_stop_and_sleep_ticks_too(env):
    fake = _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript
        class S(BotScript):
            def on_start(self, ctx): self.ctx = ctx
            def on_loop(self):
                T.events.append(("before", self.ctx.sleep(1), self.ctx.stopping))
                T.fake.gui_stop()
                T.events.append(("after", self.ctx.sleep(10_000), self.ctx.sleep_ticks(1),
                                 self.ctx.is_stop_requested()))
                return 0
    """), PID)
    assert ("before", True, False) in env.trace.events
    assert ("after", False, False, True) in env.trace.events
    assert fake.crashes == []


# --- config while running ---------------------------------------------------------------------

CONFIGURABLE = """
    import worker_trace as T
    from botwithus.api import BotScript, int_field
    class S(BotScript):
        def get_config_fields(self): return [int_field("n", "N", 1), int_field("m", "M", 2)]
        def on_start(self, ctx): self.ctx = ctx; self.loops = 0
        def on_config_update(self, config):
            T.events.append(("config", dict(config)))
            if config["n"] < 0:
                raise ValueError("n must be positive")
        def on_loop(self):
            self.loops += 1
            T.events.append(("on_loop", dict(self.ctx.config)))
            T.on_loop(self.loops)
            return -1 if self.loops >= 3 else 0
        def on_stop(self): T.events.append(("on_stop",))
"""


def test_a_set_config_is_applied_between_iterations_with_the_full_set(env):
    fake = _host(env)
    env.trace.on_loop = lambda n: n == 1 and fake.gui_set_config(5, {"n": 9})
    worker.run(_script(env, CONFIGURABLE), PID)
    ev = env.trace.events
    assert ev[ev.index(("on_loop", {"n": 1, "m": 2})) + 1] == ("config", {"n": 9, "m": 2})
    assert ("on_loop", {"n": 9, "m": 2}) in ev
    assert ("done", 5, True, "") in fake.ctl_log
    assert fake.ctl_committed == {"n": 9, "m": 2}


def test_queued_changes_apply_in_order_each_with_the_full_set(env):
    fake = _host(env)

    def push(n):
        if n == 1:
            fake.gui_set_config(1, {"n": 4})
            fake.gui_set_config(2, {"m": 8})
    env.trace.on_loop = push
    worker.run(_script(env, CONFIGURABLE), PID)
    configs = [e[1] for e in env.trace.events if e[0] == "config"]
    assert configs == [{"n": 1, "m": 2}, {"n": 4, "m": 2}, {"n": 4, "m": 8}]
    assert [e for e in fake.ctl_log if e[0] == "done"] == [
        ("done", 1, True, ""), ("done", 2, True, "")]


def test_a_config_update_that_raises_refuses_the_change_and_crashes(env):
    fake = _host(env)
    env.trace.on_loop = lambda n: n == 1 and fake.gui_set_config(3, {"n": -1})
    worker.run(_script(env, CONFIGURABLE), PID)
    assert ("done", 3, False, "ValueError: n must be positive") in fake.ctl_log
    assert fake.ctl_committed == {"n": 1, "m": 2}           # nothing committed
    assert [c["phase"] for c in fake.crashes] == ["on_config_update"]
    loops = [e for e in env.trace.events if e[0] == "on_loop"]
    assert len(loops) == 1                                  # no on_loop after the crash
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]


def test_a_set_config_during_stopping_is_never_applied(env):
    fake = _host(env)

    def stop_then_configure(n):
        fake.gui_stop()
        assert fake.gui_set_config(4, {"n": 6}) is False   # the host refuses it at once
    env.trace.on_loop = stop_then_configure
    worker.run(_script(env, CONFIGURABLE), PID)
    assert [e[1] for e in env.trace.events if e[0] == "config"] == [{"n": 1, "m": 2}]
    assert ("refused", 4) in fake.ctl_log
    assert not any(e[0] in ("take", "done") for e in fake.ctl_log)


def test_ctx_config_is_read_only(env):
    _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        from botwithus.api import BotScript, bool_field
        class S(BotScript):
            def get_config_fields(self): return [bool_field("b", "B", True)]
            def on_start(self, ctx):
                try:
                    ctx.config["b"] = False
                except TypeError:
                    T.events.append(("read_only",))
            def on_loop(self): return -1
    """), PID)
    assert ("read_only",) in env.trace.events


# --- self-running ------------------------------------------------------------------------------

def test_a_self_running_file_runs_as_main_without_attaching(env):
    fake = _host(env)
    worker.run(_script(env, """
        import worker_trace as T
        T.events.append(("body", __name__))
    """), PID)
    assert _names(env.trace.events) == ["body", "describe", "state:running", "body"]
    assert env.trace.events[0] == ("body", "bwu_script")
    assert env.trace.events[-1] == ("body", "__main__")
    assert fake.attached == 0 and fake.crashes == []


def test_a_self_running_exception_is_an_on_loop_crash(env):
    fake = _host(env)
    worker.run(_script(env, """
        if __name__ == "__main__":
            raise RuntimeError("self-run failed")
    """), PID)
    assert [(c["phase"], c["exception"]) for c in fake.crashes] == [
        ("on_loop", "RuntimeError: self-run failed")]


@pytest.mark.parametrize("code, crashed", [(None, False), (0, False), (3, True),
                                           ("no game client found", True)])
def test_system_exit_from_a_self_run(env, code, crashed):
    fake = _host(env)
    worker.run(_script(env, f"""
        if __name__ == "__main__":
            raise SystemExit({code!r})
    """), PID)
    assert bool(fake.crashes) is crashed
    if crashed:
        assert fake.crashes[0]["phase"] == "on_loop"
        assert fake.crashes[0]["exception"].startswith("SystemExit")


def test_a_self_run_sees_the_gui_stop_through_the_runner(env):
    """A self-running script that starts a ScriptRunner: the GUI's stop wakes its sleep."""
    fake = _host(env)
    env.trace.loop_entered = threading.Event()
    threading.Thread(target=lambda: (env.trace.loop_entered.wait(5), time.sleep(0.05),
                                     fake.gui_stop())).start()
    _script(env, SLEEPER, name="sleeper_for_self_run.py")   # the class lives elsewhere
    started = time.monotonic()
    worker.run(_script(env, """
        import time
        if __name__ == "__main__":
            from sleeper_for_self_run import S
            from botwithus.core.connection import Connection
            from botwithus.core.native import default_client_pid
            conn = Connection(default_client_pid())
            conn.start_script(S())
            while conn.running_scripts():
                time.sleep(0.01)
            conn.close()
    """), PID)
    sys.modules.pop("sleeper_for_self_run", None)
    assert time.monotonic() - started < LONG_SLEEP_MS / 1000.0 / 2
    assert any(e[0] == "on_stop" and e[2] for e in env.trace.events)
