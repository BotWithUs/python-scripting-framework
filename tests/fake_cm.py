"""Fakes for client management: the ``cm_*`` half of ``botwithus._native``, and an attach
backend that counts how many handles each pid has open at once.

- :class:`FakeCm` stands in for the ``cm_*`` bindings and the ``CM_*`` constants. Events are
  pushed with :meth:`FakeCm.push` and handed out by ``cm_poll_event`` exactly as the core's
  queue does (one per call, ``None`` on timeout). Failures raise :class:`FakeCmError`, which
  carries ``code``, ``result`` and ``retry_after_ms`` like the real ``CmError``.
- :class:`CountingNative` extends :class:`fakes.FakeNative` so the real ``Connection`` and
  ``ConnectionManager`` run on it. ``attach`` counts a handle as open from the moment it is
  entered (the real one has opened a pipe slot by then) until ``detach``, and can park inside
  ``attach`` for chosen pids so a test can hold a race window open.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

from fakes import FakeNative

# ADR 0007 10.2 values, under the binding's names (embed_python.c cm_add_constants).
CM_CONSTANTS = {
    "CM_STATE_QUEUED": 1, "CM_STATE_SPAWNING": 2, "CM_STATE_INJECTING": 3,
    "CM_STATE_INJECTED": 4, "CM_STATE_FAILED": 5, "CM_STATE_EXITED": 6,
    "CM_KIND_JAGEX": 1, "CM_KIND_STEAM": 2, "CM_KIND_ATTACHED": 3,
    "CM_ORIGIN_UI": 1, "CM_ORIGIN_AUTOMATION": 2,
    "CM_LIC_OK": 1, "CM_LIC_RETRYING": 2, "CM_LIC_DROPPED": 3, "CM_LIC_UNTRACKED": 4,
    "CM_EXIT_STOPPED": 1, "CM_EXIT_LICENCE": 2, "CM_EXIT_DESCRIPTOR": 3, "CM_EXIT_UNKNOWN": 4,
    "CM_STOP_GRACEFUL": 1, "CM_STOP_KILL": 2,
    "CM_ACK_CLOSING": 1, "CM_ACK_DECLINED": 2, "CM_ACK_LATER": 3,
    "CM_EV_CLIENT_STARTED": 1, "CM_EV_CLIENT_STATE": 2, "CM_EV_CLIENT_EXITED": 3,
    "CM_EV_AGENT_UPDATED": 4, "CM_EV_DATA_UPDATE_AVAILABLE": 5, "CM_EV_DATA_UPDATE_APPLIED": 6,
    "CM_EV_CLOSE_REQUESTED": 7, "CM_EV_LICENCE_STATE": 8, "CM_EV_SERVICE_SHUTTING_DOWN": 9,
    "CM_EV_SERVICE_LOST": 10, "CM_EV_SERVICE_RESTORED": 11, "CM_EV_EVENTS_DROPPED": 12,
    "CM_EV_NATIVE_UPDATE_AVAILABLE": 13, "CM_EV_NATIVE_UPDATE_APPLIED": 14,
}

# BwuResult values (bwu_host_surface.h).
RESULT_BY_CODE = {"service_unavailable": -20, "service_stopped": -21, "bad_argument": -6,
                  "internal": -8}
REFUSED = -22


class FakeCmError(RuntimeError):
    """The real ``CmError``: a ``RuntimeError`` with ``code``, ``result``, ``retry_after_ms``."""

    def __init__(self, message: str, code: str, retry_after_ms: int = -1) -> None:
        super().__init__(message)
        self.code = code
        self.result = RESULT_BY_CODE.get(code, REFUSED)
        self.retry_after_ms = retry_after_ms


def event(kind: int, **fields) -> dict:
    """A ``cm_poll_event`` dict: every field present, 0 or "" unless given."""
    raw = {"kind": kind, "pid": 0, "state": 0, "exit_code": 0, "reason": 0,
           "hosts_blocking": 0, "request_id": 0, "client_id": "", "text": ""}
    raw.update(fields)
    return raw


def client_row(client_id: str, pid: int, state: int, account_id: str = "acc-1") -> dict:
    """A ``cm_clients()`` row."""
    return {"client_id": client_id, "account_id": account_id, "account_name": "Account",
            "pid": pid, "character_index": -1, "kind": 1, "origin": 2, "state": state,
            "licence_state": 1, "licence_failures": 0, "is_agent_stale": False,
            "restart_of": 0, "started_at_ms": 1_700_000_000_000, "agent_sha": "ab" * 32}


class FakeCm:
    """The ``cm_*`` bindings. Set ``fail`` to make every call raise that code."""

    CmError = FakeCmError
    CM_SURFACE_VERSION = 1

    def __init__(self) -> None:
        for name, value in CM_CONSTANTS.items():
            setattr(self, name, value)
        self.cm_queue: queue.Queue = queue.Queue()
        self.accounts_rows: list[dict] = [{"id": "acc-1", "name": "Account"}]
        self.clients_rows: list[dict] = []
        self.fail: FakeCmError | None = None
        self.launch_hook: Callable[[str], None] | None = None
        self.clients_hook: Callable[[], None] | None = None
        self.calls: list[tuple] = []
        self.acks: list[tuple[int, int]] = []
        self.stops: list[tuple[str, int]] = []
        self._next_id = 0
        self._cm_lock = threading.Lock()

    def push(self, kind: int, **fields) -> None:
        self.cm_queue.put(event(kind, **fields))

    def _record(self, *call) -> None:
        with self._cm_lock:
            self.calls.append(call)
        if self.fail is not None:
            raise self.fail

    def cm_accounts(self):
        self._record("cm_accounts")
        return [dict(a) for a in self.accounts_rows]

    def cm_clients(self):
        self._record("cm_clients")
        if self.clients_hook is not None:
            self.clients_hook()
        return [dict(c) for c in self.clients_rows]

    def cm_launch(self, account_id, character_index=-1):
        self._record("cm_launch", account_id, character_index)
        with self._cm_lock:
            self._next_id += 1
            client_id = f"c{self._next_id}"
        if self.launch_hook is not None:
            self.launch_hook(client_id)
        return client_id

    def cm_stop(self, client_id, mode=1):
        self._record("cm_stop", client_id, mode)
        self.stops.append((client_id, mode))

    def cm_ack_close(self, request_id, decision):
        self._record("cm_ack_close", request_id, decision)
        self.acks.append((request_id, decision))

    def cm_poll_event(self, timeout_ms=0):
        try:
            return self.cm_queue.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None


class CountingNative(FakeNative):
    """``FakeNative`` whose attach counts open handles per pid and can park on a gate."""

    def __init__(self, pids: list[int] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pids = list(pids or [])
        self.open: dict[int, int] = {}
        self.max_open: dict[int, int] = {}
        self.attach_calls: dict[int, int] = {}
        self.gates: dict[int, threading.Event] = {}
        self.entered: dict[int, threading.Event] = {}
        self.discover_hook: Callable[[], None] | None = None
        self._count_lock = threading.Lock()

    def discover_pids(self):
        present = list(self.pids)
        if self.discover_hook is not None:
            self.discover_hook()
        return present

    def park(self, pid: int) -> threading.Event:
        """Make the next attach of ``pid`` wait until the returned event is set."""
        self.entered[pid] = threading.Event()
        self.gates[pid] = threading.Event()
        return self.gates[pid]

    def attach(self, pid):
        with self._count_lock:
            self.attach_calls[pid] = self.attach_calls.get(pid, 0) + 1
            self.open[pid] = self.open.get(pid, 0) + 1
            self.max_open[pid] = max(self.max_open.get(pid, 0), self.open[pid])
            gate = self.gates.pop(pid, None)
        if gate is not None:
            self.entered[pid].set()
            gate.wait(5.0)
        return super().attach(pid)

    def detach(self, handle):
        with self._count_lock:
            self.open[handle.pid] = self.open.get(handle.pid, 0) - 1
        super().detach(handle)


class CountingCmNative(CountingNative, FakeCm):
    """Both halves on one backend, as the real ``_native`` module is."""

    def __init__(self, pids: list[int] | None = None, **kwargs) -> None:
        CountingNative.__init__(self, pids, **kwargs)
        FakeCm.__init__(self)
