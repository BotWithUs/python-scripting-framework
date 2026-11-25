"""Client management for a Python management script: launch, stop and watch game clients.

This is the entry point for a *management script* -- a script that runs in ``bwu_host`` and
manages clients through the BotWithUs launcher's background service, instead of driving one
client. It sits on the native host's ``bwu_cm_*`` surface (``botwithus._native.cm_*``), which
owns the one service connection of the process, registers the host, and reconnects by itself.

    from botwithus.api.clients import ClientLauncher, StopMode, Attached
    from botwithus.host.manager import ConnectionManager

    manager = ConnectionManager()
    manager.start()
    with ClientLauncher(manager) as launcher:
        account = launcher.accounts()[0]
        handle = launcher.launch(account.id)
        outcome = handle.outcome(timeout_s=300)
        if isinstance(outcome, Attached):
            print(outcome.connection.snapshot().game_state)
        launcher.stop(handle.client_id, StopMode.GRACEFUL)

**Threads.** One dispatcher thread per launcher (``cm-dispatch``) drains the process's event
queue. For each event it updates the launcher's own client table, hands an injected client
this launcher launched to the :class:`ConnectionManager`, then copies the event to every
subscriber. A script reads events from :meth:`ClientLauncher.events` (a blocking iterator with
a non-blocking ``poll``), or registers :meth:`ClientLauncher.on_event` callbacks, which run on
the dispatcher thread and must stay short. Only one launcher may run per process: the native
queue is process-wide and single-consumer, and a second reader would silently split it.

**Close requests.** When a data update waits for this host to close, the service sends a close
request. With a handler registered through :meth:`ClientLauncher.on_close_requested`, the
handler runs on its own short-lived thread; returning a :class:`CloseDecision` acks with it,
returning ``None`` sends nothing (call :meth:`ClientLauncher.ack_close` later yourself). With
**no handler, nothing is acked and nothing exits** -- "no answer" is a normal state, and the
update applies by itself once the host exits. The framework never exits the process, not even
after ``CLOSING``: exiting is the script's job.

**After the service comes back** (:class:`ServiceRestored`) or after missed events
(:class:`EventsDropped`), nothing is replayed. Call :meth:`ClientLauncher.clients` and rebuild
whatever view the script keeps. The launcher does this for its own pending launches already.
"""
from __future__ import annotations

import atexit
import collections
import enum
import itertools
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol, Union

from ..core import ctl as _ctl
from ..core import native as _native_seam

_log = logging.getLogger("botwithus.clients")

#: The ``bwu_cm_*`` group this module is written against (``CM_SURFACE_VERSION``).
CM_SURFACE_VERSION = 1
#: How long one dispatcher poll waits in native code, GIL released.
_POLL_MS = 250
#: Back-off after a failing poll (only a process without the native core sees one).
_POLL_FAILURE_BACKOFF_S = 1.0
#: How long ``close`` waits for the dispatcher to finish its current poll.
_JOIN_TIMEOUT_S = 5.0
#: Events one subscriber may fall behind before its oldest is dropped (the core's own cap).
SUBSCRIBER_QUEUE_CAP = 1024

#: Failure code of a pending launch whose client was gone, failed or exited when the launcher
#: resynchronised after a service outage or dropped events (the transition itself was missed).
LOST_DURING_OUTAGE = "lost_during_outage"
#: Failure code of a launch whose process exited before it was injected.
EXITED_BEFORE_INJECTED = "exited"
#: Failure code of a launch still pending when the launcher was closed. The client itself is
#: not stopped; it keeps running and the scan may still attach it.
LAUNCHER_CLOSED = "launcher_closed"


# ----------------------------------------------------------------------------- enumerations
# Values are the ABI's (native-scripting-host include/bwu_host_surface.h, ADR 0007 10.2).
# start() checks each one against the backend's constant of the same name. 0 means "none or
# unknown" on the wire; a value no member covers is kept as a plain int, never an error.

class ClientState(enum.IntEnum):
    QUEUED = 1
    SPAWNING = 2
    INJECTING = 3
    INJECTED = 4
    FAILED = 5
    EXITED = 6


class ClientKind(enum.IntEnum):
    JAGEX = 1
    STEAM = 2
    ATTACHED = 3


class Origin(enum.IntEnum):
    UI = 1
    AUTOMATION = 2


class LicenceState(enum.IntEnum):
    OK = 1
    RETRYING = 2
    DROPPED = 3
    UNTRACKED = 4


class ExitReason(enum.IntEnum):
    STOPPED = 1
    LICENCE = 2
    DESCRIPTOR = 3
    UNKNOWN = 4


class StopMode(enum.IntEnum):
    """How :meth:`ClientLauncher.stop` stops a client (wire ``"close"`` / ``"kill"``)."""
    GRACEFUL = 1
    KILL = 2


class CloseDecision(enum.IntEnum):
    """An answer to a close request."""
    CLOSING = 1
    DECLINED = 2
    LATER = 3


class EventKind(enum.IntEnum):
    CLIENT_STARTED = 1
    CLIENT_STATE = 2
    CLIENT_EXITED = 3
    AGENT_UPDATED = 4
    DATA_UPDATE_AVAILABLE = 5
    DATA_UPDATE_APPLIED = 6
    CLOSE_REQUESTED = 7
    LICENCE_STATE = 8
    SERVICE_SHUTTING_DOWN = 9
    SERVICE_LOST = 10
    SERVICE_RESTORED = 11
    EVENTS_DROPPED = 12
    NATIVE_UPDATE_AVAILABLE = 13
    NATIVE_UPDATE_APPLIED = 14


# backend constant prefix -> enum; member NAME is appended (CM_STATE_QUEUED, ...).
_CONSTANT_PREFIXES: tuple[tuple[str, type[enum.IntEnum]], ...] = (
    ("CM_STATE_", ClientState), ("CM_KIND_", ClientKind), ("CM_ORIGIN_", Origin),
    ("CM_LIC_", LicenceState), ("CM_EXIT_", ExitReason), ("CM_STOP_", StopMode),
    ("CM_ACK_", CloseDecision), ("CM_EV_", EventKind),
)


def _enum_or_int(cls: type[enum.IntEnum], value: int) -> Union[enum.IntEnum, int]:
    try:
        return cls(value)
    except ValueError:
        return int(value)


def _text_or_none(text: str) -> str | None:
    return text if text else None


# ----------------------------------------------------------------------------- records

@dataclass(frozen=True, slots=True)
class Account:
    """A launcher account. The service never returns a password, PIN or token here."""
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Client:
    """One client the service tracks (``cm_clients()``)."""
    client_id: str
    account_id: str
    account_name: str
    pid: int
    character_index: int
    kind: Union[ClientKind, int]
    origin: Union[Origin, int]
    state: Union[ClientState, int]
    licence_state: Union[LicenceState, int]
    licence_failures: int
    is_agent_stale: bool
    restart_of: int
    started_at_ms: int
    agent_sha: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Client":
        return cls(
            client_id=raw["client_id"], account_id=raw["account_id"],
            account_name=raw["account_name"], pid=int(raw["pid"]),
            character_index=int(raw["character_index"]),
            kind=_enum_or_int(ClientKind, raw["kind"]),
            origin=_enum_or_int(Origin, raw["origin"]),
            state=_enum_or_int(ClientState, raw["state"]),
            licence_state=_enum_or_int(LicenceState, raw["licence_state"]),
            licence_failures=int(raw["licence_failures"]),
            is_agent_stale=bool(raw["is_agent_stale"]), restart_of=int(raw["restart_of"]),
            started_at_ms=int(raw["started_at_ms"]), agent_sha=raw["agent_sha"],
        )


# ----------------------------------------------------------------------------- events

@dataclass(frozen=True, slots=True)
class ClientStarted:
    """A launch reached its first record, or auto-restart replaced the process (new pid)."""
    client_id: str
    pid: int
    state: Union[ClientState, int]
    account_id: str


@dataclass(frozen=True, slots=True)
class ClientStateChanged:
    """A client's launch moved on. ``code`` is set only for ``FAILED``; ``message`` otherwise."""
    client_id: str
    state: Union[ClientState, int]
    code: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class ClientExited:
    client_id: str
    exit_code: int
    reason: Union[ExitReason, int]


@dataclass(frozen=True, slots=True)
class AgentUpdated:
    """The launcher has a new agent; clients injected before it are now stale."""
    sha: str


@dataclass(frozen=True, slots=True)
class DataUpdateAvailable:
    staged_sha: str | None
    hosts_blocking: int


@dataclass(frozen=True, slots=True)
class DataUpdateApplied:
    sha: str


@dataclass(frozen=True, slots=True)
class NativeUpdateAvailable:
    """The native host package (not data.zip) changed state. No host ever blocks it."""
    staged_sha: str | None


@dataclass(frozen=True, slots=True)
class NativeUpdateApplied:
    """A native host package version is now current. Running hosts keep their own version."""
    sha: str


@dataclass(frozen=True, slots=True)
class CloseRequested:
    """The service asks this host to close so a data update can apply."""
    request_id: int
    hosts_blocking: int
    reason: str


@dataclass(frozen=True, slots=True)
class LicenceChanged:
    """The licence link changed (``client_id`` None) or one client's licence did."""
    link: str
    client_id: str | None
    state: Union[LicenceState, int, None]


@dataclass(frozen=True, slots=True)
class ServiceShuttingDown:
    close_clients: bool


@dataclass(frozen=True, slots=True)
class ServiceLost:
    """The host lost the service; calls fail until :class:`ServiceRestored`."""
    code: str


@dataclass(frozen=True, slots=True)
class ServiceRestored:
    """The host has the service again. Nothing is replayed: call ``clients()``."""


@dataclass(frozen=True, slots=True)
class EventsDropped:
    """Events were missed (the core's queue, the service's sequence, or this subscriber)."""
    count: int


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    """An event kind this framework does not know (a newer host). Passed through as is."""
    kind: int
    raw: dict


LauncherEvent = Union[
    ClientStarted, ClientStateChanged, ClientExited, AgentUpdated, DataUpdateAvailable,
    DataUpdateApplied, NativeUpdateAvailable, NativeUpdateApplied, CloseRequested,
    LicenceChanged, ServiceShuttingDown, ServiceLost, ServiceRestored, EventsDropped,
    UnknownEvent,
]


def _client_state_event(raw: dict) -> ClientStateChanged:
    state = _enum_or_int(ClientState, raw["state"])
    text = _text_or_none(raw["text"])
    failed = state == ClientState.FAILED
    return ClientStateChanged(raw["client_id"], state, text if failed else None,
                              None if failed else text)


def _licence_event(raw: dict) -> LicenceChanged:
    state = _enum_or_int(LicenceState, raw["state"]) if raw["state"] else None
    return LicenceChanged(raw["text"], _text_or_none(raw["client_id"]), state)


def _dropped_event(raw: dict) -> EventsDropped:
    try:
        return EventsDropped(int(raw["text"]))
    except ValueError:
        return EventsDropped(0)


_DECODERS: dict[int, Callable[[dict], LauncherEvent]] = {
    EventKind.CLIENT_STARTED: lambda r: ClientStarted(
        r["client_id"], int(r["pid"]), _enum_or_int(ClientState, r["state"]), r["text"]),
    EventKind.CLIENT_STATE: _client_state_event,
    EventKind.CLIENT_EXITED: lambda r: ClientExited(
        r["client_id"], int(r["exit_code"]), _enum_or_int(ExitReason, r["reason"])),
    EventKind.AGENT_UPDATED: lambda r: AgentUpdated(r["text"]),
    EventKind.DATA_UPDATE_AVAILABLE: lambda r: DataUpdateAvailable(
        _text_or_none(r["text"]), int(r["hosts_blocking"])),
    EventKind.DATA_UPDATE_APPLIED: lambda r: DataUpdateApplied(r["text"]),
    EventKind.NATIVE_UPDATE_AVAILABLE: lambda r: NativeUpdateAvailable(_text_or_none(r["text"])),
    EventKind.NATIVE_UPDATE_APPLIED: lambda r: NativeUpdateApplied(r["text"]),
    EventKind.CLOSE_REQUESTED: lambda r: CloseRequested(
        int(r["request_id"]), int(r["hosts_blocking"]), r["text"]),
    EventKind.LICENCE_STATE: _licence_event,
    EventKind.SERVICE_SHUTTING_DOWN: lambda r: ServiceShuttingDown(r["text"] == "close_clients"),
    EventKind.SERVICE_LOST: lambda r: ServiceLost(r["text"]),
    EventKind.SERVICE_RESTORED: lambda r: ServiceRestored(),
    EventKind.EVENTS_DROPPED: _dropped_event,
}


def decode_event(raw: dict) -> LauncherEvent:
    """Turn one ``cm_poll_event`` dict into its typed event."""
    decoder = _DECODERS.get(int(raw["kind"]))
    return decoder(raw) if decoder is not None else UnknownEvent(int(raw["kind"]), dict(raw))


# ----------------------------------------------------------------------------- errors

class ClientsError(RuntimeError):
    """A client-management call failed. Branch on :attr:`code` (launcher ADR 0007 2.6).

    Subclasses ``RuntimeError``, as ``botwithus._native.CmError`` does, so existing
    ``except RuntimeError`` callers keep working. ``__cause__`` is the original ``CmError``.
    """

    def __init__(self, message: str, code: str, result: int = 0, retry_after_ms: int = -1):
        super().__init__(message)
        self.message = message
        self.code = code
        self.result = result
        self.retry_after_ms = retry_after_ms


class ServiceDown(ClientsError):
    """No service answered. The host keeps reconnecting by itself; it never starts one."""


class ServiceUnavailable(ServiceDown):
    """``service_unavailable``: not running, restarting, crashed, or no answer in 40 s."""


class ServiceStopped(ServiceDown):
    """``service_stopped``: the user stopped the service from the tray."""


class RateLimited(ClientsError):
    """``rate_limited``: retry after :attr:`retry_after_ms` milliseconds."""


class BadArgument(ClientsError):
    """``bad_argument``: a bad argument. Nothing was sent to the service."""


class Refused(ClientsError):
    """The service answered with an error code (``account_not_found``, ``session_limit``...)."""


_ERROR_CLASSES: dict[str, type[ClientsError]] = {
    "service_unavailable": ServiceUnavailable,
    "service_stopped": ServiceStopped,
    "rate_limited": RateLimited,
    "bad_argument": BadArgument,
}


def _typed_error(exc: BaseException) -> ClientsError:
    code = getattr(exc, "code", "") or ""
    cls = _ERROR_CLASSES.get(code, Refused)
    return cls(str(exc), code, int(getattr(exc, "result", 0) or 0),
               int(getattr(exc, "retry_after_ms", -1)))


def _bad_argument(message: str) -> BadArgument:
    return BadArgument(message, "bad_argument")


def _require_member(cls: type[enum.IntEnum], value: Any, what: str) -> enum.IntEnum:
    # bool is an int: True would otherwise pass as CLOSING / GRACEFUL. Refuse it outright.
    # A member of another enum (CloseDecision.CLOSING == 1) is refused too.
    wrong_enum = isinstance(value, enum.Enum) and not isinstance(value, cls)
    if isinstance(value, bool) or not isinstance(value, int) or wrong_enum:
        raise _bad_argument(f"{what} must be a {cls.__name__}, not {value!r}")
    try:
        return cls(value)
    except ValueError:
        raise _bad_argument(f"{what} {value!r} is not a {cls.__name__}") from None


# ----------------------------------------------------------------------------- launches

@dataclass(frozen=True, slots=True)
class Attached:
    """The client was injected and is attached; ``connection`` is the manager's."""
    connection: Any


@dataclass(frozen=True, slots=True)
class Failed:
    """The launch failed before it was injected (the service's failure ``code``)."""
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AttachFailed:
    """The client was injected but attaching to it failed."""
    message: str


LaunchOutcome = Union[Attached, Failed, AttachFailed]


class LaunchHandle:
    """A queued launch. :meth:`outcome` says how it ended."""

    def __init__(self, client_id: str, sequence: int) -> None:
        self.client_id = client_id
        self._sequence = sequence
        self._done = threading.Event()
        self._outcome: LaunchOutcome | None = None

    def done(self) -> bool:
        return self._done.is_set()

    def outcome(self, timeout_s: float | None = None) -> LaunchOutcome:
        """Block until the launch ends. Raises ``TimeoutError`` if ``timeout_s`` passes.

        In a GUI worker it raises :class:`~botwithus.core.ctl.StopRequested` within
        about 100 ms of the GUI stopping the script, instead of waiting on."""
        done = _ctl.wait_stop_aware(self._done.wait, timeout_s)
        if done is None:
            raise _ctl.StopRequested(f"stopped while waiting for launch {self.client_id}")
        if not done:
            raise TimeoutError(f"launch {self.client_id} still pending after {timeout_s} s")
        return self._outcome  # type: ignore[return-value]

    def _complete(self, outcome: LaunchOutcome) -> None:
        self._outcome = outcome
        self._done.set()

    def __repr__(self) -> str:
        return f"LaunchHandle({self.client_id!r}, outcome={self._outcome!r})"


class ClientAdopter(Protocol):
    """What the launcher hands an injected client to (``ConnectionManager.adopt``)."""

    def adopt(self, pid: int) -> Any: ...


@dataclass(slots=True)
class _Tracked:
    pid: int
    state: Union[ClientState, int]
    fail_code: str | None = None


# ----------------------------------------------------------------------------- subscriptions

class Subscription:
    """A subscriber's own queue of events: iterate it (blocking) or :meth:`poll` it.

    It holds at most :data:`SUBSCRIBER_QUEUE_CAP` events. On overflow the oldest is dropped,
    and the next read returns one :class:`EventsDropped` with the count first.
    """

    def __init__(self, on_close: Callable[["Subscription"], None],
                 cap: int = SUBSCRIBER_QUEUE_CAP) -> None:
        self._queue: collections.deque = collections.deque()
        self._cap = cap
        self._dropped = 0
        self._closed = False
        self._cond = threading.Condition()
        self._on_close = on_close

    def _put(self, event: LauncherEvent) -> None:
        with self._cond:
            if self._closed:
                return
            if len(self._queue) >= self._cap:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(event)
            self._cond.notify_all()

    def _take(self) -> LauncherEvent | None:
        if self._dropped:
            count, self._dropped = self._dropped, 0
            return EventsDropped(count)
        return self._queue.popleft() if self._queue else None

    def _ready(self) -> bool:
        return bool(self._queue or self._dropped or self._closed)

    def poll(self, timeout_s: float = 0.0) -> LauncherEvent | None:
        """The next event, waiting up to ``timeout_s``; ``None`` if none came or closed.
        In a GUI worker a stop also ends the wait, with ``None``."""
        with self._cond:
            _ctl.wait_stop_aware(lambda t: self._cond.wait_for(self._ready, t), timeout_s)
            return self._take()

    def __iter__(self) -> Iterator[LauncherEvent]:
        return self

    def __next__(self) -> LauncherEvent:
        """The next event, blocking. Iteration ends when the subscription closes, or, in a
        GUI worker, when the GUI stops the script."""
        with self._cond:
            _ctl.wait_stop_aware(lambda t: self._cond.wait_for(self._ready, t), None)
            event = self._take()
        if event is None:
            raise StopIteration
        return event

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._on_close(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Registration:
    """Closes an ``on_event`` callback registration."""

    def __init__(self, on_close: Callable[[], None]) -> None:
        self._on_close = on_close

    def close(self) -> None:
        self._on_close()

    def __enter__(self) -> "_Registration":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ----------------------------------------------------------------------------- the launcher

# rule-exception: process-level guard. The native event queue it protects is itself
# process-global and single-consumer; two launchers would silently split events between them.
_PROCESS_GUARD = threading.Lock()

CloseHandler = Callable[[CloseRequested], Union[CloseDecision, None]]


class ClientLauncher:
    """Launch, stop and watch clients through the launcher service. See the module docs.

    ``manager`` receives each injected client this launcher launched (normally the
    :class:`botwithus.host.manager.ConnectionManager`). ``backend`` defaults to the native
    seam (:func:`botwithus.core.native.backend`); tests pass a fake.
    """

    def __init__(self, manager: ClientAdopter, backend: Any = None) -> None:
        self._manager = manager
        self._backend_override = backend
        self._lock = threading.Lock()
        self._table: dict[str, _Tracked] = {}
        self._pending: dict[str, LaunchHandle] = {}
        self._sequence = itertools.count(1)
        self._subscribers: list[Subscription] = []
        self._callbacks: list[Callable[[LauncherEvent], None]] = []
        self._close_handler: CloseHandler | None = None
        self._undelivered_close: CloseRequested | None = None
        self._latest_close: CloseRequested | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------

    def _be(self) -> Any:
        return self._backend_override if self._backend_override is not None \
            else _native_seam.backend()

    def start(self) -> "ClientLauncher":
        """Start the dispatcher. Raises if another launcher already runs in this process."""
        if self._thread is not None:
            return self
        be = self._be()
        _check_surface(be)
        if not _PROCESS_GUARD.acquire(blocking=False):
            raise RuntimeError("a ClientLauncher is already running in this process; "
                               "share it instead of starting a second one")
        self._stop.clear()
        self._thread = threading.Thread(target=self._dispatch_loop, name="cm-dispatch",
                                        daemon=True)
        self._thread.start()
        atexit.register(self.close)
        return self

    def close(self) -> None:
        """Stop the dispatcher, end every subscription, and free the process slot."""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        if thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_S)
        self._thread = None
        atexit.unregister(self.close)
        _PROCESS_GUARD.release()
        for sub in self._subscribers_snapshot():
            sub.close()
        with self._lock:
            orphaned, self._pending = list(self._pending.values()), {}
        for handle in orphaned:
            handle._complete(Failed(LAUNCHER_CLOSED, "the launcher closed before the "
                                                     "launch finished"))

    def __enter__(self) -> "ClientLauncher":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- calls ---------------------------------------------------------------

    def _call(self, name: str, *args: Any) -> Any:
        be = self._be()
        cm_error = getattr(be, "CmError", None)
        try:
            return getattr(be, name)(*args)
        except Exception as exc:
            if isinstance(cm_error, type) and isinstance(exc, cm_error):
                raise _typed_error(exc) from exc
            raise

    def accounts(self) -> list[Account]:
        """The launcher's accounts, in the service's order (at most 256 rows; see docs)."""
        return [Account(a["id"], a["name"]) for a in self._call("cm_accounts")]

    def clients(self) -> list[Client]:
        """Every client the service tracks (at most 128 rows). The source of truth."""
        return [Client.from_raw(c) for c in self._call("cm_clients")]

    def resync(self) -> list[Client]:
        """What to call after :class:`ServiceRestored` or :class:`EventsDropped`.

        The same list as :meth:`clients`. The launcher's own table and pending launches are
        resynchronised by the dispatcher already; this is for the script's own view.
        """
        return self.clients()

    def launch(self, account_id: str, character_index: int = -1) -> LaunchHandle:
        """Queue a launch. Returns once the service has queued it (this can take ~30 s).

        Needs :meth:`start`. The handle's :meth:`LaunchHandle.outcome` completes when the
        client is attached (:class:`Attached`), fails (:class:`Failed`), or is injected but
        cannot be attached (:class:`AttachFailed`). Raises :class:`RateLimited`,
        :class:`Refused` (``session_limit``, ``concurrency_cap``, ...) or :class:`ServiceDown`.
        """
        if self._thread is None:
            raise RuntimeError("start() the launcher before launching: the dispatcher "
                               "completes the launch")
        if not isinstance(account_id, str) or not account_id:
            raise _bad_argument(f"account_id must be a non-empty str, not {account_id!r}")
        client_id = self._call("cm_launch", account_id, int(character_index))
        with self._lock:
            handle = LaunchHandle(client_id, next(self._sequence))
            self._pending[client_id] = handle
        return handle

    def stop(self, client_id: str, mode: StopMode = StopMode.GRACEFUL) -> None:
        """Stop any client, whoever launched it. A :class:`ClientExited` follows."""
        member = _require_member(StopMode, mode, "mode")
        self._call("cm_stop", client_id, int(member))

    # --- close requests ------------------------------------------------------

    @property
    def pending_close_request(self) -> CloseRequested | None:
        """The latest close request not yet acked through this launcher, if any."""
        with self._lock:
            return self._latest_close

    def on_close_requested(self, handler: CloseHandler | None) -> None:
        """Register (or clear, with ``None``) the close-request handler.

        The handler runs on its own thread. Return a :class:`CloseDecision` to ack with it,
        or ``None`` to send nothing now. An exception, or any other return value, is logged
        and sends nothing. A request that arrived while no handler was registered is
        delivered once to the next handler registered, unless it was acked meanwhile.
        """
        with self._lock:
            self._close_handler = handler
            request, self._undelivered_close = (
                (self._undelivered_close, None) if handler is not None
                else (None, self._undelivered_close))
        if request is not None:
            self._spawn_close_handler(handler, request)

    def ack_close(self, request_id: int, decision: CloseDecision) -> None:
        """Answer a close request. Changes only what the launcher shows; nothing exits."""
        member = _require_member(CloseDecision, decision, "decision")
        self._call("cm_ack_close", int(request_id), int(member))
        with self._lock:
            if self._latest_close is not None and self._latest_close.request_id == request_id:
                self._latest_close = None
            if (self._undelivered_close is not None
                    and self._undelivered_close.request_id == request_id):
                self._undelivered_close = None

    def _on_close_request(self, event: CloseRequested) -> None:
        with self._lock:
            self._latest_close = event
            handler = self._close_handler
            self._undelivered_close = None if handler is not None else event
        if handler is None:
            _log.info("close request %s: no handler registered; not answering",
                      event.request_id)
            return
        self._spawn_close_handler(handler, event)

    def _spawn_close_handler(self, handler: CloseHandler, event: CloseRequested) -> None:
        threading.Thread(target=self._run_close_handler, args=(handler, event),
                         name=f"cm-close-{event.request_id}", daemon=True).start()

    def _run_close_handler(self, handler: CloseHandler, event: CloseRequested) -> None:
        try:
            decision = handler(event)
        except Exception:
            _log.exception("close handler raised for request %s; not answering",
                           event.request_id)
            return
        if decision is None:
            return
        if not isinstance(decision, CloseDecision):
            _log.error("close handler returned %r for request %s, not a CloseDecision; "
                       "not answering", decision, event.request_id)
            return
        try:
            self.ack_close(event.request_id, decision)
        except ClientsError as exc:
            _log.warning("ack for close request %s failed: %s (%s)",
                         event.request_id, exc.code, exc)

    # --- subscriptions -------------------------------------------------------

    def events(self) -> Subscription:
        """A new subscription to every event dispatched from now on."""
        sub = Subscription(self._remove_subscriber)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def on_event(self, callback: Callable[[LauncherEvent], None]) -> _Registration:
        """Call ``callback`` for each event, **on the dispatcher thread**: keep it short.

        A slow callback delays every launch hand-off and every other subscriber. Its
        exceptions are logged and swallowed. Close the returned registration to stop.
        """
        with self._lock:
            self._callbacks.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return _Registration(remove)

    def _remove_subscriber(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def _subscribers_snapshot(self) -> list[Subscription]:
        with self._lock:
            return list(self._subscribers)

    def _fan_out(self, event: LauncherEvent) -> None:
        with self._lock:
            subs, callbacks = list(self._subscribers), list(self._callbacks)
        for sub in subs:
            sub._put(event)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                _log.exception("on_event callback raised; ignored")

    # --- the dispatcher ------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            raw = self._poll_once()
            if raw is not None:
                self._handle(raw)
            self._advance_launches()

    def _poll_once(self) -> dict | None:
        try:
            return self._call("cm_poll_event", _POLL_MS)
        except ClientsError as exc:
            _log.debug("cm_poll_event failed: %s (%s)", exc.code, exc)
        except Exception:
            _log.exception("cm_poll_event raised")
        self._stop.wait(_POLL_FAILURE_BACKOFF_S)
        return None

    def _handle(self, raw: dict) -> None:
        event = decode_event(raw)
        self._track(event)
        if isinstance(event, (ServiceRestored, EventsDropped)):
            self._resync_table()
        self._advance_launches()
        if isinstance(event, CloseRequested):
            self._on_close_request(event)
        self._fan_out(event)

    def _track(self, event: LauncherEvent) -> None:
        with self._lock:
            if isinstance(event, ClientStarted):
                self._table[event.client_id] = _Tracked(event.pid, event.state)
            elif isinstance(event, ClientStateChanged):
                tracked = self._table.setdefault(event.client_id, _Tracked(0, event.state))
                tracked.state, tracked.fail_code = event.state, event.code
            elif isinstance(event, ClientExited):
                tracked = self._table.setdefault(event.client_id, _Tracked(0, 0))
                tracked.state = ClientState.EXITED
            else:
                return
            needs_pid = event.client_id if self._table[event.client_id].pid == 0 else None
        if needs_pid is not None and isinstance(event, ClientStateChanged) \
                and event.state == ClientState.INJECTED:
            self._fill_pid(needs_pid)

    def _fill_pid(self, client_id: str) -> None:
        """CLIENT_STATE carries pid 0; look it up when CLIENT_STARTED was missed."""
        try:
            rows = self.clients()
        except ClientsError as exc:
            _log.warning("pid lookup for %s failed: %s", client_id, exc.code)
            return
        with self._lock:
            for row in rows:
                if row.client_id == client_id and client_id in self._table:
                    self._table[client_id].pid = row.pid

    def _resync_table(self) -> None:
        """Rebuild the table from ``cm_clients()``; fail pending launches that were lost."""
        with self._lock:
            horizon = next(self._sequence)  # handles registered after this are not judged
        try:
            rows = self.clients()
        except ClientsError as exc:
            _log.warning("resync failed: %s; pending launches wait for the next one", exc.code)
            return
        with self._lock:
            self._table = {r.client_id: _Tracked(r.pid, r.state) for r in rows}
            lost = [h for cid, h in self._pending.items()
                    if h._sequence < horizon and self._is_lost(cid)]
            for handle in lost:
                del self._pending[handle.client_id]
        for handle in lost:
            handle._complete(Failed(LOST_DURING_OUTAGE,
                                    "the client failed, exited or vanished while events "
                                    "were missed"))

    def _is_lost(self, client_id: str) -> bool:
        tracked = self._table.get(client_id)
        return tracked is None or tracked.state in (ClientState.FAILED, ClientState.EXITED)

    def _advance_launches(self) -> None:
        """Finish every pending launch whose client reached a final state."""
        for handle, tracked in self._ready_launches():
            if tracked.state == ClientState.INJECTED:
                handle._complete(self._hand_off(handle.client_id, tracked.pid))
            elif tracked.state == ClientState.FAILED:
                handle._complete(Failed(tracked.fail_code or "failed", "the launch failed"))
            else:
                handle._complete(Failed(EXITED_BEFORE_INJECTED,
                                        "the process exited before it was injected"))

    def _ready_launches(self) -> list[tuple[LaunchHandle, _Tracked]]:
        ready = []
        with self._lock:
            for client_id, handle in list(self._pending.items()):
                tracked = self._table.get(client_id)
                if tracked is None or not _is_final(tracked):
                    continue
                del self._pending[client_id]
                ready.append((handle, _Tracked(tracked.pid, tracked.state, tracked.fail_code)))
        return ready

    def _hand_off(self, client_id: str, pid: int) -> LaunchOutcome:
        try:
            connection = self._manager.adopt(pid)
        except Exception as exc:
            _log.warning("client %s (pid %s) injected but attach failed: %s",
                         client_id, pid, exc)
            return AttachFailed(str(exc))
        _log.info("client %s (pid %s) injected and attached", client_id, pid)
        return Attached(connection)


def _is_final(tracked: _Tracked) -> bool:
    if tracked.state == ClientState.INJECTED:
        return tracked.pid != 0
    return tracked.state in (ClientState.FAILED, ClientState.EXITED)


def _check_surface(be: Any) -> None:
    version = getattr(be, "CM_SURFACE_VERSION", None)
    if version != CM_SURFACE_VERSION:
        raise _native_seam.ProtocolMismatch(
            f"native host offers client-management surface {version}, this package is "
            f"written for {CM_SURFACE_VERSION}")
    for prefix, cls in _CONSTANT_PREFIXES:
        for member in cls:
            native_value = getattr(be, prefix + member.name, None)
            if native_value is not None and native_value != member.value:
                raise _native_seam.ProtocolMismatch(
                    f"{prefix}{member.name} is {native_value} in the native host, "
                    f"{member.value} here")
