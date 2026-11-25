"""Per-script runtime: one daemon thread per script, `on_start`/`on_loop`/`on_stop`.

Mirrors the Java and C# hosts: there is no host-wide tick loop. Each script is its own
self-timed loop -- ``on_loop`` returns the delay in milliseconds until the next call (or
a negative value to stop), and the runner sleeps that long on an interruptible event so a
stop is observed within one iteration.

Before ``on_start`` the runner checks the script's config fields and calls
``on_config_update`` once with their defaults, exactly as a GUI worker does with the saved
values, so a script behaves the same under ``bwu_host --py`` and in the GUI. Inside a GUI
worker (a self-running script that starts a runner) a :class:`StopWatcher` mirrors the GUI's
stop onto the runner's stop event, so the script's sleeps return early.
"""
from __future__ import annotations

import enum
import logging
import threading
from typing import Callable

from ..api import config as _config
from ..api.script import BotScript, ScriptContext, manifest_of
from ..api.timing import bound_context
from .ctl import StopWatcher, validate_cli
from .native import ClientExited
from .runlog import ScriptRun

_log = logging.getLogger("botwithus.runtime")

# How long ScriptRunner.stop() waits, by default, for the script's thread to end.
STOP_JOIN_S = 5.0


class ScriptState(enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class ScriptRunner:
    """Runs one ``BotScript`` on its own thread against a ``ScriptContext``."""

    def __init__(self, script: BotScript, ctx: ScriptContext, pid: int = 0,
                 on_exit: Callable[[], None] | None = None) -> None:
        self._script = script
        self._on_exit = on_exit  # run last on the script's thread, after the ui release
        self._ctx = ctx
        self._pid = pid  # the attached client: gives the run log's slot
        self._stop = ctx._stop  # shared stop event
        self._thread: threading.Thread | None = None
        self._initial: dict = {}
        self.state = ScriptState.STOPPED

    @property
    def name(self) -> str:
        return self._ctx.name

    @property
    def thread(self) -> threading.Thread | None:
        """The script's thread, once started."""
        return self._thread

    @property
    def thread_alive(self) -> bool:
        """True while the script's thread is still running (it may outlive :meth:`stop`)."""
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> None:
        """Start the script's thread. Raises :class:`~botwithus.api.config.ConfigSchemaError`
        first, without starting anything, when the script's config fields are invalid."""
        if self._thread is not None:
            return
        self._initial = self._initial_config()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"script-{self.name}", daemon=True)
        self.state = ScriptState.RUNNING
        self._thread.start()

    def stop(self, join_timeout_s: float | None = None) -> None:
        """Ask the script to stop and wait up to ``join_timeout_s`` (default
        :data:`STOP_JOIN_S`) for its thread.

        The state is STOPPING while the thread is still running, and STOPPED (or ERROR, after
        a crash) once it has finished, including when it had finished before this call."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            if self.state is ScriptState.RUNNING:
                self.state = ScriptState.STOPPING
            # Never wait for the script from its own thread, nor from inside one of its ui
            # handlers: its thread waits for that handler before on_stop, so the join would
            # only run out its timeout.
            if t is not threading.current_thread() and not self._ctx._delivering_ui_here():
                t.join(STOP_JOIN_S if join_timeout_s is None else join_timeout_s)
        if (t is None or not t.is_alive()) and self.state is not ScriptState.ERROR:
            self.state = ScriptState.STOPPED

    def _initial_config(self) -> dict:
        """The script's defaults, after checking its manifest and fields the way the host
        checks a GUI script's description (in CLI mode, through the host's own check)."""
        desc = {"manifest": manifest_of(type(self._script)).to_wire(),
                "config_schema": _config.schema_to_wire(self._script.get_config_fields())}
        return validate_cli(desc)

    def _run(self) -> None:
        watcher = StopWatcher(self._stop).start()
        try:
            with bound_context(self._ctx):
                self._run_script()
        finally:
            # After on_stop: this script's plugin-UI components, and no other script's.
            self._ctx._release_ui()
            watcher.close()
            if self._on_exit is not None:
                self._on_exit()

    def _run_script(self) -> None:
        # Opened on this thread, so the script's prints, logging and host calls go to the run.
        run = ScriptRun.open(manifest_of(type(self._script)), self._script, self._pid)
        run.state("STARTING", "RUNNING")
        try:
            self._script.on_config_update(self._ctx._set_config(self._initial))
        except ClientExited as exc:
            # on_start never ran, so neither does on_stop; the run ends stopped, not ERROR.
            _log.info("script %s: client exited during on_config_update: %s", self.name, exc)
            self.state = ScriptState.STOPPED
            run.state("RUNNING", "STOPPED")
            run.close()
            return
        except Exception as exc:
            self.state = ScriptState.ERROR
            _log.exception("script %s crashed in on_config_update", self.name)
            run.state("RUNNING", "ERROR")
            run.crash("on_config_update", 0, exc)
            run.close()
            return
        phase, iteration = "on_start", 0
        try:
            self._script.on_start(self._ctx)
            phase = "on_loop"
            while not self._ctx.is_stop_requested():
                if not self._ctx._await_resumed():
                    break  # stopped (or the client exited) while paused
                iteration += 1
                delay = self._script.on_loop()
                if delay is None or delay < 0:
                    break
                if not self._ctx.sleep(delay):
                    break  # stop requested, or the client exited, during the sleep
        except ClientExited as exc:
            # The client went away under the script: it ends stopped, not crashed.
            _log.info("script %s: client exited during %s: %s", self.name, phase, exc)
        except Exception as exc:
            self.state = ScriptState.ERROR
            _log.exception("script %s crashed in %s", self.name, phase)
            run.state("RUNNING", "ERROR")
            run.crash(phase, iteration if phase == "on_loop" else 0, exc)
        finally:
            self._ctx._close_ui_delivery()   # no ui handler runs during or after on_stop
            try:
                self._script.on_stop()
            except ClientExited as exc:
                _log.info("script %s: client exited during on_stop: %s", self.name, exc)
            except Exception as exc:
                _log.exception("script %s crashed in on_stop", self.name)
                run.crash("on_stop", 0, exc)
            if self.state is not ScriptState.ERROR:
                self.state = ScriptState.STOPPED
                run.state("RUNNING", "STOPPED")
            run.close()
