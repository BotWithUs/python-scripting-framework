"""Auto-attach connection manager -- one host process, every game client at once.

A background scan loop discovers attachable client pids (through the native host), attaches
a ``Connection`` per client (each with its own native host handle, RPC, event pump, and
script runners), and sweeps dead ones. Mirrors the Java/C# hosts' auto-attach.

**One attach per pid, whoever finds it first.** A client launched through
:class:`botwithus.api.clients.ClientLauncher` is handed here with :meth:`ConnectionManager.adopt`
as soon as the launcher service reports it injected, while the scan may find the same pid on
its own. The ``_connections`` key is the claim: the membership check, the attach and the insert
run under ``_lock`` on both paths, so the loser of the race gets the winner's ``Connection`` and
never opens a second one (the Python form of the Java host's claimed-pid set). Discovery runs
under the same lock for the same reason: a pid list read before an adopt and reconciled after
it would detach the connection that was just adopted.

Navigation needs no per-manager setup anymore: each ``Connection`` routes pathing through
its own native host (``bwu_path`` -> the host's linked worldwalker), so there is no shared
ctypes artifact to load or own here.
"""
from __future__ import annotations

import logging
import threading

from ..core.connection import Connection
from ..core.native import discover_pids

_log = logging.getLogger("botwithus.host")


class ConnectionManager:
    def __init__(self, scan_interval_s: float = 2.0, enable_navigation: bool = True) -> None:
        self._scan_interval = scan_interval_s
        self._connections: dict[int, Connection] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scanner: threading.Thread | None = None
        self._enable_navigation = enable_navigation

    # --- scanning ------------------------------------------------------------

    def start(self) -> None:
        if self._scanner is not None:
            return
        self._scan_once()
        self._scanner = threading.Thread(target=self._scan_loop, name="auto-attach", daemon=True)
        self._scanner.start()

    def _scan_loop(self) -> None:
        while not self._stop.wait(self._scan_interval):
            self._scan_once()

    def _scan_once(self) -> None:
        with self._lock:
            # Inside the lock on purpose: see the module docstring (the adopt race).
            present = set(discover_pids())
            known = set(self._connections)
            for pid in present - known:
                self._attach_logged(pid)
            for pid in known - present:
                self._detach(pid)
            for pid, conn in list(self._connections.items()):
                if not conn.alive:
                    self._detach(pid)

    def _attach_logged(self, pid: int) -> None:
        try:
            self._attach(pid)
        except Exception as exc:
            _log.warning("failed to attach pid %s: %s", pid, exc)

    def _attach(self, pid: int) -> Connection:
        """Open and record a connection. The caller holds ``_lock``. Raises on failure."""
        conn = Connection(pid, enable_navigation=self._enable_navigation)
        self._connections[pid] = conn
        _log.info("attached client pid %s", pid)
        return conn

    def adopt(self, pid: int) -> Connection:
        """Attach ``pid`` now, or return the connection the scan already opened for it.

        The hand-off for a client launched through the launcher service. It never opens a
        second connection for a pid: the check and the attach happen under the lock the scan
        also holds. It raises whatever the attach raises (``OSError`` for no client or no
        snapshot mapping, ``ProtocolMismatch`` for a version skew); the scan still retries
        that pid on its own schedule.
        """
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("connection manager is closed")
            existing = self._connections.get(pid)
            if existing is not None and existing.alive:
                return existing
            if existing is not None:
                self._detach(pid)
            return self._attach(pid)

    def _detach(self, pid: int) -> None:
        conn = self._connections.pop(pid, None)
        if conn is not None:
            conn.close()
            _log.info("detached client pid %s", pid)

    # --- accessors -----------------------------------------------------------

    def connections(self) -> list[Connection]:
        with self._lock:
            return list(self._connections.values())

    def connection(self, pid: int) -> Connection | None:
        with self._lock:
            return self._connections.get(pid)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for conn in self._connections.values():
                conn.close()
            self._connections.clear()
