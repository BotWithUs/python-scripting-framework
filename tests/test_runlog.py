"""The runner's run-log glue: phases, the iteration, state crumbs, ``ctx.log``, and close.

The sink, the redactor, the header and the stdout/logging capture are native (tested in
native-scripting-host, and end to end through a real ``bwu_host`` run); here a recording
backend stands in for ``botwithus._native.runlog_*`` and the real ``ScriptRunner`` drives it.
"""
from __future__ import annotations

import logging
import threading

import pytest

from botwithus.api.script import BotScript, ScriptContext, script_manifest
from botwithus.core import native as native_mod
from botwithus.core.runtime import ScriptRunner, ScriptState

from fakes import FakeNative

PID = 4242


class RunlogNative(FakeNative):
    """FakeNative plus a recorder for the native run-log calls."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.opens: list[dict] = []
        self.crumbs: list[tuple[int, str, str]] = []
        self.crashes: list[tuple] = []
        self.writes: list[tuple] = []
        self.closes: list[int] = []
        self.binds: list[int] = []

    def runlog_open(self, name, version=None, author=None, path=None, pid=0):
        self.opens.append({"name": name, "version": version, "author": author, "path": path,
                           "pid": pid, "thread": threading.current_thread().name})
        return 11

    def runlog_crumb(self, run, kind, detail):
        self.crumbs.append((run, kind, detail))

    def runlog_crash(self, run, phase, iteration, exception, top_frame, trace):
        self.crashes.append((run, phase, iteration, exception, top_frame, trace))
        return len(self.crashes) == 1   # the native side keeps a run's first crash block only

    def runlog_write(self, run, level, thread, logger, message, trace=None):
        self.writes.append((run, level, thread, logger, message, trace))

    def runlog_close(self, run):
        self.closes.append(run)

    def runlog_bind(self, run):
        self.binds.append(run)


@pytest.fixture
def native():
    fake = RunlogNative(pid=PID)
    native_mod.set_backend(fake)
    yield fake
    native_mod.set_backend(None)


def make_script(fail_in: str | None = None, at: int = 1, also_stop: bool = False):
    @script_manifest(name="Crashy", version="1.2", author="spec")
    class Crashy(BotScript):
        loops = 0

        def on_start(self, ctx):
            self.ctx = ctx
            if fail_in == "on_start":
                raise RuntimeError("start broke")

        def on_loop(self):
            type(self).loops += 1
            if fail_in == "on_loop" and type(self).loops == at:
                raise ValueError("loop broke")
            return 0 if type(self).loops < 5 else -1

        def on_stop(self):
            if fail_in == "on_stop" or also_stop:
                raise KeyError("stop broke")

    return Crashy()


def run_to_end(script) -> ScriptRunner:
    ctx = ScriptContext("Crashy", game=None, events=None, stop_event=threading.Event())
    runner = ScriptRunner(script, ctx, pid=PID)
    runner.start()
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()
    return runner


def test_a_clean_run_opens_on_the_script_thread_records_state_and_closes(native):
    runner = run_to_end(make_script())
    assert runner.state is ScriptState.STOPPED
    (o,) = native.opens
    assert (o["name"], o["version"], o["author"], o["pid"]) == ("Crashy", "1.2", "spec", PID)
    assert o["path"].endswith("test_runlog.py"), "the script's file is hashed"
    assert o["thread"] == "script-Crashy", "opened on the runner thread, which binds it"
    assert [c[1:] for c in native.crumbs] == [("state", "STARTING->RUNNING"),
                                             ("state", "RUNNING->STOPPED")]
    assert native.crashes == []
    assert native.closes == [11]


def test_an_on_loop_error_is_phase_on_loop_with_its_iteration(native):
    runner = run_to_end(make_script("on_loop", at=3))
    assert runner.state is ScriptState.ERROR
    (crash,) = native.crashes
    run, phase, iteration, exception, top_frame, trace = crash
    assert (run, phase, iteration) == (11, "on_loop", 3)
    assert exception == "ValueError: loop broke"
    assert "Traceback (most recent call last)" in trace and "on_loop" in trace
    assert native.crumbs[-1][1:] == ("state", "RUNNING->ERROR"), "state change precedes the block"
    assert native.closes == [11]


def test_an_on_start_error_is_labelled_on_start_not_on_loop(native, caplog):
    caplog.set_level(logging.ERROR, logger="botwithus.runtime")
    run_to_end(make_script("on_start"))
    (crash,) = native.crashes
    assert crash[1:3] == ("on_start", 0)
    assert "crashed in on_start" in caplog.text
    assert "crashed in on_loop" not in caplog.text, "the old mislabel"


def test_an_on_stop_error_is_phase_on_stop(native):
    runner = run_to_end(make_script("on_stop"))
    (crash,) = native.crashes
    assert crash[1:4] == ("on_stop", 0, "KeyError: 'stop broke'")
    assert runner.state is ScriptState.STOPPED, "an on_stop failure does not mark the run ERROR"


def test_a_failure_after_the_crash_block_is_kept_as_an_error_line(native):
    run_to_end(make_script("on_loop", at=1, also_stop=True))
    assert [c[1] for c in native.crashes] == ["on_loop", "on_stop"]
    (write,) = native.writes
    assert write[1] == 40 and write[4].startswith("on_stop failed: KeyError")


def test_ctx_log_is_a_logger_for_the_script(native, caplog):
    caplog.set_level(logging.DEBUG, logger="script.Crashy")
    ctx = ScriptContext("Crashy", game=None, events=None, stop_event=threading.Event())
    ctx.log.debug("d %s", 1)
    ctx.log.info("i")
    ctx.log.warn("w")
    ctx.log.error("e")
    got = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert got == [("script.Crashy", "DEBUG", "d 1"), ("script.Crashy", "INFO", "i"),
                   ("script.Crashy", "WARNING", "w"), ("script.Crashy", "ERROR", "e")]


def test_without_the_runlog_surface_the_runner_is_unchanged():
    native_mod.set_backend(FakeNative(pid=PID))   # no runlog_* at all
    try:
        runner = run_to_end(make_script("on_loop", at=2))
        assert runner.state is ScriptState.ERROR
    finally:
        native_mod.set_backend(None)
