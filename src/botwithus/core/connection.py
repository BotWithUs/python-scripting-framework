"""One ``Connection`` per game client pid -- the assembly point for a single client.

Bundles a native host handle (the attached client), the ``GameApi``, the event bus, a
background event pump that drains the ring into the bus, and the script runners. Every part
is pid-scoped; a connection never migrates to another pid.

Since the native host owns the transport, this class no longer opens a pipe or maps shared
memory or runs an RPC reader thread -- it holds one ``NativeHost`` and reads snapshots, RPC,
and events through it. The event pump is a daemon thread polling the ring at ~50 Hz and
publishing decoded events onto the bus inline (mirrors the Java host's ``event-pump-<pid>``).
"""
from __future__ import annotations

import logging
import threading

from ..api.events import EventBus, ScriptEventBus, ServerTick
from ..api.game import GameApi
from ..api.navigation import SceneWalker
from ..api.orientation import WireOrientationDecoder
from ..api.script import BotScript, ScriptContext, manifest_of
from ..api.ui import PluginUi, UiRouter
from .change_detector import SnapshotChangeDetector
from .client_exit import ClientExitWatch
from .events import EventRingReader
from .native import HostDetached, NativeHost, discover_pids
from .rpc import RpcClient
from .runtime import ScriptRunner
from .variables import VariableReader
from .snapshot import SnapshotView
from .ui_events import UiEventThread

_log = logging.getLogger("botwithus.connection")

# How long close() waits for the event pump to finish its current poll. The pump wakes every
# poll interval (20 ms by default), so this is only reached when a poll itself is stuck; the
# host's own detach then still refuses to free the handle under it.
PUMP_JOIN_S = 2.0


class Connection:
    """A live connection to one game client."""

    # A class default too: the threads that call _finish_close on exit (the pump among them)
    # must find it even on a connection built without __init__, as a unit test may build one.
    _detach_deferred = False

    def __init__(self, pid: int, poll_interval_s: float = 0.02,
                 enable_navigation: bool = True) -> None:
        self.pid = pid
        self._host = NativeHost.attach(pid)
        self._rpc = RpcClient(self._host)
        self.game = GameApi(self._rpc, self._current_snapshot, VariableReader(self._host))
        self.events = EventBus()
        self._ring = EventRingReader(self._host)
        self._changes = SnapshotChangeDetector(self.events.publish)
        self._orientations = WireOrientationDecoder()  # one per session: log a bad facing once
        self._runners: dict[str, ScriptRunner] = {}
        # Plugin UI: one PluginUi per script context. Several scripts can share this one pipe,
        # so each tracks and releases its own components, and the router hands each ui event
        # to the script(s) holding its handle. The events are polled on their own thread (not
        # the ring pump: a ui poll waits on the host's I/O lock), only while a script uses the UI.
        self._ui_router = UiRouter()
        self._ui_events = UiEventThread(self._host, self._ui_router, pid, poll_interval_s,
                                        on_exit=self._finish_close)
        self._alive = True
        # close() from inside a ui handler cannot free the host handle at once: that handler's
        # script waits for it before on_stop, and the ui thread is the caller. The detach is
        # then deferred to whichever of those threads ends last (_finish_close on exit).
        self._detach_deferred = False
        self._close_lock = threading.Lock()
        self._freed = False
        # Navigation routes through the host's linked worldwalker (bwu_path) -- no ctypes
        # here anymore. It shares this connection's native host, so it starts from the
        # player's live tile like every other read.
        self._navigation = SceneWalker(self.game, self._host) if enable_navigation else None

        self._pump_stop = threading.Event()
        self._pump = threading.Thread(
            target=self._pump_loop, args=(poll_interval_s,),
            name=f"event-pump-{pid}", daemon=True,
        )
        self._pump.start()

    # --- snapshot ------------------------------------------------------------

    def _current_snapshot(self) -> SnapshotView:
        return SnapshotView(self._host.snapshot_bytes(), self._orientations)

    def snapshot(self) -> SnapshotView:
        return self._current_snapshot()

    # --- scripts -------------------------------------------------------------

    def start_script(self, script: BotScript) -> str:
        name = manifest_of(type(script)).name
        if name in self._runners and self._runners[name].state.name == "RUNNING":
            raise ValueError(f"script {name!r} already running on pid {self.pid}")
        runner = ScriptRunner(script, self.new_context(name, threading.Event()), pid=self.pid,
                              on_exit=self._finish_close)
        runner.start()  # raises ConfigSchemaError, starting nothing, for bad config fields
        self._runners[name] = runner
        _log.info("started script %s on pid %s", name, self.pid)
        return name

    def new_context(self, name: str, stop_event: threading.Event) -> ScriptContext:
        """A ``ScriptContext`` for a script on this client, stopped through ``stop_event``."""
        events = ScriptEventBus(self.events)
        ctx = ScriptContext(name, self.game, events, stop_event)
        ctx.navigation = self._navigation
        ui = PluginUi(self._host, on_used=self._ui_events.wanted, router=self._ui_router)
        ui._events_bus = events
        ui._stop_requested = ctx.is_stop_requested   # ui delivery ends once a stop is asked
        ctx.ui = ui
        ctx._exit_watch = ClientExitWatch(self)
        return ctx

    def stop_script(self, name: str) -> None:
        runner = self._runners.get(name)
        if runner is not None:
            runner.stop()

    def running_scripts(self) -> list[str]:
        return [n for n, r in self._runners.items() if r.state.name == "RUNNING"]

    # --- event pump ----------------------------------------------------------

    def _pump_loop(self, interval_s: float) -> None:
        try:
            self._pump_until_stopped(interval_s)
        finally:
            self._finish_close()

    def _pump_until_stopped(self, interval_s: float) -> None:
        while not self._pump_stop.wait(interval_s):
            if not self._alive:
                return
            try:
                ticked = False
                for evt in self._ring.poll():
                    self.events.publish(evt)
                    ticked = ticked or isinstance(evt, ServerTick)
                if ticked:
                    self._observe_changes()
            except HostDetached:
                return  # closed under the pump; nothing was passed down
            except Exception:
                _log.exception("event pump failed on pid %s", self.pid)
                return

    def _observe_changes(self) -> None:
        """Once per server tick: derive inventory / skill change events from a fresh frame. A
        frame that cannot be read drops the baseline instead of ending the pump."""
        try:
            self._changes.observe(self._current_snapshot())
        except HostDetached:
            raise
        except Exception:  # noqa: BLE001
            _log.debug("change detection skipped a tick on pid %s", self.pid, exc_info=True)
            self._changes.observe_state(None)

    # --- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive and self._host.alive

    def snapshot_readable(self) -> bool:
        """True when the native host could take a fresh frame from the client now.

        This asks ``refresh``, not ``snapshot_bytes``: after the client exits the host keeps
        its view mapped and the reads keep returning the last frame, so only ``refresh``
        turns False. A host that cannot refresh at all counts as not readable."""
        try:
            return self._host.refresh()
        except Exception:
            return False

    def client_listed(self) -> bool:
        """True while the native host still lists this client as attachable."""
        try:
            return self.pid in discover_pids()
        except Exception:
            return False

    def close(self) -> bool:
        """Stop the event pump and every script, then free the host handle.

        The handle is freed only after the pump thread and every script thread have ended,
        so no thread can make a host call on it afterwards. A script thread still running
        after :meth:`ScriptRunner.stop`'s wait keeps the handle attached (logged): a bounded
        leak, never a free under a live thread. True when the handle was freed.

        Called from inside a ui handler (the ui event thread), it returns False at once, and
        the handle is freed by whichever ends last of that handler's script thread and the ui
        event thread, after the handler has returned. Every other script is stopped (and
        waited for) as usual."""
        self._alive = False
        self._pump_stop.set()
        if self._pump is not threading.current_thread():
            self._pump.join(PUMP_JOIN_S)
        for runner in list(self._runners.values()):
            runner.stop()
        self._ui_events.close(PUMP_JOIN_S)
        if self._ui_router.delivering_here():
            self._detach_deferred = True
            if self._finish_close():
                return True
            _log.info("pid %s: close() from a ui handler; the host handle is freed when %s "
                      "end", self.pid, ", ".join(self._busy_threads()))
            return False
        running = [r.name for r in self._runners.values() if r.thread_alive]
        if running:
            _log.warning("pid %s: script thread(s) %s still running; leaving the host handle "
                         "attached rather than free it under them", self.pid, ", ".join(running))
            return False
        return self._host.detach()

    def _busy_threads(self) -> list[str]:
        """The threads that can still call the host, other than the calling one. The ui event
        thread counts while it runs, even on itself: it frees the handle on exit instead."""
        me = threading.current_thread()
        busy = [f"script {r.name}" for r in list(self._runners.values())
                if r.thread_alive and r.thread is not me]
        if self._ui_events.running:
            busy.append("ui events")
        if self._pump.is_alive() and self._pump is not me:
            busy.append("event pump")
        return busy

    def _finish_close(self) -> bool:
        """The deferred detach of a close() from a ui handler: free the host handle once no
        other thread can call it. Run on exit by each script thread, the ui event thread and
        the pump. Exactly one detach; True once the handle is freed."""
        if not self._detach_deferred or self._busy_threads():
            return False
        with self._close_lock:
            if not self._freed:
                self._freed = self._host.detach()
            return self._freed
