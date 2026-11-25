"""The plugin-UI event thread of one client connection.

``ui_poll_events`` takes the native host's I/O lock, the one every pipe call on this client
holds, for as long as a call is in flight (up to the 5 s RPC timeout). So it must never run on
the connection's ring pump: one script's slow ui call would stall ServerTick, chat and varp
delivery for every script on the client. It gets its own thread instead, which:

- starts on the first ui call any script makes (:meth:`UiEventThread.wanted`);
- polls every ``interval_s`` (``ui_poll_events`` never waits, so this paces it);
- routes what it gets through the connection's :class:`~botwithus.api.ui.UiRouter`, with each
  distinct poll or routing failure caught and logged once, so a bad event never ends the thread;
- ends once no script that used the UI is still live, after one last poll that drains what the
  host kept, and starts again on the next first use;
- ends for good on :meth:`close`, on a lost client, or once the host is detached.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ..api.ui import UiRouter
from .native import HostDetached, NativeHost

_log = logging.getLogger("botwithus.ui_events")

UI_POLL_INTERVAL_S = 0.02


class UiEventThread:
    """Polls ``ui_poll_events`` for one connection while any script there uses the UI."""

    def __init__(self, host: NativeHost, router: UiRouter, pid: int,
                 interval_s: float = UI_POLL_INTERVAL_S,
                 on_exit: Callable[[], None] | None = None) -> None:
        self._host = host
        self._on_exit = on_exit   # run last on the thread, once it no longer counts as running
        self._router = router
        self._pid = pid
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dead = False          # the client is gone or the connection closed: never restart
        self._reported: set[str] = set()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None

    def wanted(self) -> None:
        """A script made its first ui call: make sure the thread runs."""
        with self._lock:
            if self._thread is not None or self._dead or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._loop, name=f"ui-events-{self._pid}",
                                            daemon=True)
            self._thread.start()

    def close(self, join_s: float) -> None:
        """End the thread for good and wait up to ``join_s`` for it."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(join_s)

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._interval_s):
                if not self._router.live():
                    # Every UI script is released. One last poll drains what the host kept (a
                    # stale overflow included), so the next script does not inherit it.
                    if not self._poll_once():
                        return
                    with self._lock:
                        if not self._router.live():
                            self._thread = None
                            return
                    continue   # a script started using the UI during that poll
                if not self._poll_once():
                    return
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            if self._on_exit is not None:
                self._on_exit()

    def is_current(self) -> bool:
        """True on the ui event thread itself."""
        with self._lock:
            return self._thread is threading.current_thread()

    def _poll_once(self) -> bool:
        """One poll and route. False when the thread must end."""
        try:
            raw = self._host.ui("poll_events")
        except HostDetached:
            self._dead = True
            return False
        except ConnectionError as exc:   # ClientExited included
            _log.info("pid %s: ui event polling stopped: %s", self._pid, exc)
            self._dead = True
            return False
        except Exception as exc:
            self._log_once("ui_poll_events failed", exc)
            return True
        try:
            self._router.route(raw)
        except Exception as exc:
            self._log_once("routing ui events failed", exc)
        return True

    def _log_once(self, what: str, exc: BaseException) -> None:
        """Log each distinct failure once: the same one every 20 ms would flood the log."""
        key = f"{what}|{type(exc).__name__}|{exc}"
        if key not in self._reported:
            self._reported.add(key)
            _log.exception("pid %s: %s", self._pid, what)
