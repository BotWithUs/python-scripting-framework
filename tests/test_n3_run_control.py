"""N3: pause and the settings store through the GUI worker's run control (native ABI 8).

The fake host (tests/fakes.py) plays bwu_host's half: one pause switch with a sequence number
that the GUI (gui_set_paused) and the script (ctl_set_paused) share, and a settings store. Each
test names what would count as a failure.
"""
import threading
import time

import pytest

from botwithus import worker
from botwithus.core import ctl
from botwithus.core import native as native_mod

from fakes import FakeNative
from test_worker import PID, _host, _script, env  # noqa: F401  (env is a fixture)

HOLD_S = 0.3
WAIT_S = 5

COUNTER = """
    import worker_trace as T
    from botwithus.api import BotScript, int_field
    class S(BotScript):
        def get_config_fields(self): return [int_field("n", "N", 1)]
        def on_start(self, ctx):
            self.ctx = ctx
            T.ctx = ctx
            T.loops = 0
        def on_loop(self):
            T.loops += 1
            T.on_loop(self)
            return 20
"""


def _wait(predicate, seconds=WAIT_S):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_gui_pause_holds_the_loop_and_a_resume_releases_it(env):
    # Failure: on_loop keeps running while the GUI has paused the run, a resume does not
    # release it, or the pause is reported to the GUI as the script's.
    fake = _host(env)
    env.trace.on_loop = lambda s: None
    seen = {}

    def gui():
        try:   # the GUI thread always stops the run, so a bug fails the test instead of hanging it
            seen["running"] = _wait(lambda: getattr(env.trace, "loops", 0) >= 2)
            fake.gui_set_paused(True)
            seen["held"] = _wait(lambda: env.trace.ctx.is_paused)
            held = env.trace.loops
            time.sleep(HOLD_S)
            seen["during"] = env.trace.loops - held
            fake.gui_set_paused(False)
            seen["after"] = _wait(lambda: env.trace.loops >= held + 3)
        finally:
            fake.gui_stop()

    t = threading.Thread(target=gui)
    t.start()
    worker.run(_script(env, COUNTER), PID)
    t.join()
    assert seen == {"running": True, "held": True, "during": 0, "after": True}
    assert [e for e in fake.ctl_log if e[0] == "paused"] == [("paused", True, "control"),
                                                          ("paused", False, "control")]
    assert fake.crashes == []


def test_a_script_pause_reaches_the_gui_and_the_gui_can_resume_it(env):
    # Failure: ctx.pause() not told to the host (the GUI never sees it), or a GUI resume that
    # does not end a pause the script made.
    fake = _host(env)

    def on_loop(script):
        if env.trace.loops == 2:
            script.ctx.pause()
    env.trace.on_loop = on_loop

    seen = {}

    def gui():
        try:   # always stop the run: a bug fails the test instead of hanging it
            seen["host saw it"] = _wait(lambda: fake.ctl_paused()[0])
            held = getattr(env.trace, "loops", 0)
            time.sleep(HOLD_S)
            seen["during"] = getattr(env.trace, "loops", 0) - held
            fake.gui_set_paused(False)
            seen["resumed"] = _wait(lambda: env.trace.loops >= held + 2)
        finally:
            fake.gui_stop()

    t = threading.Thread(target=gui)
    t.start()
    worker.run(_script(env, COUNTER), PID)
    t.join()
    assert seen == {"host saw it": True, "during": 0, "resumed": True}
    assert [e for e in fake.ctl_log if e[0] == "paused"] == [("paused", True, "script"),
                                                          ("paused", False, "control")]
    assert not env.trace.ctx.is_paused


SETTINGS = """
    import worker_trace as T
    from botwithus.api import BotScript, int_field
    class S(BotScript):
        def get_config_fields(self): return [int_field("n", "N", 1)]
        def on_start(self, ctx):
            T.seen = ctx.settings.as_dict()
            ctx.settings.put("route", "north")
            ctx.settings.put_int("trips", 12)
            T.saved = ctx.settings.save()
            T.fake.save_result = (T.fake.CTL_SAVE_FAILED, "disk full")
            T.failed = ctx.settings.save()
        def on_loop(self): return -1
"""


def test_settings_start_from_the_saved_map_with_the_config_over_it_and_save(env, caplog):
    # Failure: the saved map not seeding ctx.settings, a saved declared key winning over the
    # config in force (Java merges the applied config over), save() not reaching the store,
    # or a refused save reported as kept / not logged.
    fake = _host(env, ctl_saved_settings={"route": "south", "n": "7"}, ctl_saved_config={"n": 3})
    worker.run(_script(env, SETTINGS), PID)
    assert env.trace.seen == {"route": "south", "n": "3"}
    assert env.trace.saved is True and env.trace.failed is False
    assert fake.saves[0] == {"route": "north", "n": "3", "trips": "12"}
    assert any("settings not saved: disk full" in r.getMessage() for r in caplog.records)


def test_cli_mode_has_no_store_and_keeps_pause_local(env):
    # Failure: a CLI run claiming a save, or a CLI pause sent to a host with no GUI.
    fake = _host(env, mode=FakeNative.CTL_CLI)
    native_mod.set_backend(fake)
    assert ctl.save_settings({"a": "b"})[0] == ctl.SAVE_UNSUPPORTED
    assert ctl.saved_settings() is None
    assert ctl.paused() == (False, 0)
    watcher = ctl.PauseWatcher(lambda flag: pytest.fail("no watcher thread in CLI mode")).start()
    watcher.close()


def test_a_host_older_than_abi_8_behaves_as_no_store():
    # Failure: an AttributeError from a backend that has ctl_mode but not the ABI 8 calls.
    class Old:
        CTL_WORKER = 1

        def ctl_mode(self):
            return 1

    native_mod.set_backend(Old())
    try:
        assert ctl.paused() == (False, 0) and ctl.set_paused(True) is True
        assert ctl.save_settings({"a": "b"})[0] == ctl.SAVE_UNSUPPORTED
        assert ctl.saved_settings() is None
    finally:
        native_mod.set_backend(None)
