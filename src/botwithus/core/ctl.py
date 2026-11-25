"""The seam onto the native host's GUI control surface (``botwithus._native.ctl_*``).

When ``bwu_host`` runs a script for the GUI (``bwu_host --worker`` / ``--describe``), it owns a
private control pipe to the GUI. The framework never sees that pipe: it reports the script's
description and phases, takes config changes, and waits stop-aware, through the functions here
(``docs/GUI-WORKER-PROTOCOL.md`` section 9.2, native-scripting-host).

Every function works in every mode. Under ``bwu_host --py`` (CLI mode) the host's own calls are
no-ops: ``wait`` is a plain sleep, ``stopping`` is False, ``take_config`` is None. Without the
surface at all (an older host, or the unit suite's fake without it) this module behaves as CLI
mode, with :func:`describe` validating in Python and returning the defaults.

Only :mod:`botwithus.worker` calls :func:`describe`, :func:`state`, :func:`take_config` and
:func:`config_done`. The ordinary runners call only :func:`wait` and :func:`stopping`.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..api import config as _config
from . import native as _native_seam

MODE_CLI = 0
MODE_WORKER = 1
MODE_DESCRIBE = 2

WAKE_TIMEOUT = "timeout"
WAKE_STOP = "stop"

# ctl_wait takes a uint32 millisecond count.
_WAIT_MAX_MS = 0xFFFFFFFF
# How long a stop watcher waits per call. ctl_wait returns at once on stop, so this bounds only
# how long the watcher takes to notice that its run is over, not the stop latency.
WATCH_PERIOD_MS = 250


# How often a stop-aware wait outside a script's own sleep checks for a stop.
STOP_POLL_S = 0.1


class StopRequested(RuntimeError):
    """A blocking wait gave up because the GUI asked this script to stop (or went away).
    Only raised in a GUI worker; under ``bwu_host --py`` nothing raises it."""


class CtlError(RuntimeError):
    """A control-surface call failed: ``"<key path>: <detail>"`` for an invalid description,
    or an illegal phase. The native binding raises its own ``CtlError`` (also a
    ``RuntimeError``); :func:`describe` and :func:`state` re-raise it as this class."""


def _surface() -> Any:
    """The native module when it has the ctl surface, else None (behave as CLI)."""
    try:
        be = _native_seam.backend()
    except _native_seam.NativeUnavailable:
        return None
    return be if hasattr(be, "ctl_mode") else None


def mode() -> int:
    """:data:`MODE_CLI`, :data:`MODE_WORKER` or :data:`MODE_DESCRIBE`."""
    be = _surface()
    if be is None:
        return MODE_CLI
    m = int(be.ctl_mode())
    if m == int(be.CTL_WORKER):
        return MODE_WORKER
    if m == int(be.CTL_DESCRIBE):
        return MODE_DESCRIBE
    return MODE_CLI


def describe(desc: Mapping[str, Any]) -> dict[str, Any]:
    """Report the script's description; returns the initial full config. Raises
    :class:`CtlError` when the description is invalid (the run then ends as a schema crash)."""
    be = _surface()
    if be is None:
        try:
            _config.validate_describe(desc)
        except _config.ConfigSchemaError as exc:
            raise CtlError(str(exc)) from exc
        return _config.defaults(desc["config_schema"])
    try:
        return dict(be.ctl_describe(dict(desc)))
    except RuntimeError as exc:
        if isinstance(exc, CtlError):
            raise
        raise CtlError(str(exc)) from exc


def state(phase: str) -> None:
    """Report ``"attaching"``, ``"running"`` or ``"stopping"``. No-op outside a worker."""
    be = _surface()
    if be is None:
        return
    try:
        be.ctl_state(phase)
    except RuntimeError as exc:
        raise CtlError(str(exc)) from exc


def wait(timeout_ms: int) -> str:
    """Block up to ``timeout_ms``, or until stop: :data:`WAKE_TIMEOUT` or :data:`WAKE_STOP`."""
    ms = min(max(0, int(timeout_ms)), _WAIT_MAX_MS)
    be = _surface()
    if be is None:
        time.sleep(ms / 1000.0)
        return WAKE_TIMEOUT
    return str(be.ctl_wait(ms))


def stopping() -> bool:
    """True once the GUI has asked this worker to stop (or has gone away). Never blocks."""
    be = _surface()
    return bool(be.ctl_stopping()) if be is not None else False


def stop_requested() -> bool:
    """For a self-running script: True once the GUI has asked it to stop. Always False under
    ``bwu_host --py``. Plugin scripts use ``ctx.stopping`` instead."""
    return stopping()


def wait_stop_aware(wait: Callable[[float | None], bool], timeout_s: float | None) -> bool | None:
    """Run a blocking ``wait(timeout_s) -> bool`` so that a GUI stop ends it.

    Returns ``wait``'s result, ``False`` on timeout, or ``None`` when a stop came first. In CLI
    mode it is exactly ``wait(timeout_s)``; in a worker the wait is sliced into
    :data:`STOP_POLL_S` pieces with a stop check between them."""
    if mode() == MODE_CLI:
        return wait(timeout_s)
    deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
    while True:
        if stopping():
            return None
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return bool(wait(0))
        piece = STOP_POLL_S if remaining is None else min(STOP_POLL_S, remaining)
        if wait(piece):
            return True


def _is_twice(exc: BaseException) -> bool:
    return "called twice" in str(exc)


def validate_cli(desc: Mapping[str, Any]) -> dict[str, Any]:
    """CLI-mode check of a script's description, returning its defaults: the host's
    ``ctl_describe`` (which in CLI mode validates and sends nothing), with the framework's own
    validator when the host has no ctl surface or has already been asked once in this
    process. Raises :class:`~botwithus.api.config.ConfigSchemaError`."""
    be = _surface()
    if be is not None and mode() == MODE_CLI:
        try:
            return dict(be.ctl_describe(dict(desc)))
        except RuntimeError as exc:
            if not _is_twice(exc):
                raise _config.ConfigSchemaError(str(exc)) from exc
    _config.validate_describe(desc)
    return _config.defaults(desc["config_schema"])


def take_config() -> tuple[int, dict[str, Any]] | None:
    """The oldest queued config change as ``(id, full typed set)``, or None."""
    be = _surface()
    if be is None:
        return None
    taken = be.ctl_take_config()
    if taken is None:
        return None
    change_id, cfg = taken
    return int(change_id), dict(cfg)


def config_done(change_id: int, ok: bool, message: str = "") -> None:
    """Report how ``on_config_update`` went for a change from :func:`take_config`."""
    be = _surface()
    if be is not None:
        be.ctl_config_done(int(change_id), bool(ok), message)


class StopWatcher:
    """Mirrors the host's stop flag onto a runner's stop event.

    A daemon thread loops on :func:`wait`; when it returns :data:`WAKE_STOP` it sets the event,
    so ``ctx.sleep`` and ``ctx.sleep_ticks`` (which wait on that event) return at once. In CLI
    mode there is no host stop to mirror and no thread is started. :meth:`close` ends the
    thread and joins it.
    """

    __slots__ = ("_event", "_done", "_thread")

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "StopWatcher":
        if self._thread is None and mode() != MODE_CLI:
            self._thread = threading.Thread(target=self._watch, name="bwu-ctl-stop-watch",
                                            daemon=True)
            self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._done.is_set():
            if wait(WATCH_PERIOD_MS) == WAKE_STOP:
                self._event.set()
                return

    def close(self) -> None:
        self._done.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join()
        self._thread = None


# --- pause and settings (native ABI 8; GUI-WORKER-PROTOCOL.md 5.4, 6.6, 9.10) ----------------

SAVE_OK = 1
SAVE_FAILED = 0
SAVE_UNSUPPORTED = -1
# How long ctx.settings.save() waits for the GUI's answer.
SAVE_TIMEOUT_MS = 5000
# How long the pause watcher waits per call; a change wakes it at once.
PAUSE_WATCH_PERIOD_MS = 250


def _control_surface() -> Any:
    """The native module when it has the ABI 8 run control, else None."""
    be = _surface()
    return be if be is not None and hasattr(be, "ctl_paused") else None


def paused() -> tuple[bool, int]:
    """The run's pause switch and its sequence number. ``(False, 0)`` outside a GUI worker."""
    be = _control_surface()
    if be is None:
        return False, 0
    flag, seq = be.ctl_paused()
    return bool(flag), int(seq)


def set_paused(flag: bool) -> bool:
    """Tell the host the script paused or resumed, so the GUI shows it. A no-op (True) outside
    a GUI worker; False once the run is terminating."""
    be = _control_surface()
    return True if be is None else bool(be.ctl_set_paused(bool(flag)))


def wait_pause(timeout_ms: int, since_seq: int) -> tuple[int, bool]:
    """Block until the switch changes from ``since_seq``, stop, or the timeout: ``(seq, paused)``."""
    be = _control_surface()
    if be is None:
        time.sleep(max(0, int(timeout_ms)) / 1000.0)
        return 0, False
    seq, flag = be.ctl_wait_pause(min(max(0, int(timeout_ms)), _WAIT_MAX_MS), int(since_seq))
    return int(seq), bool(flag)


def saved_settings() -> dict[str, str] | None:
    """The settings an earlier run of this script saved (the GUI's ``welcome.settings``)."""
    be = _control_surface()
    found = be.ctl_settings() if be is not None else None
    return None if found is None else {str(k): str(v) for k, v in found.items()}


def save_settings(values: Mapping[str, str], timeout_ms: int = SAVE_TIMEOUT_MS) -> tuple[int, str]:
    """Send the whole settings map to the GUI's store: ``(SAVE_OK | SAVE_FAILED |
    SAVE_UNSUPPORTED, why)``."""
    be = _control_surface()
    if be is None:
        return SAVE_UNSUPPORTED, "no settings store outside a GUI worker on a native host of ABI 8"
    code, message = be.ctl_save_settings({str(k): str(v) for k, v in values.items()}, int(timeout_ms))
    return int(code), str(message)


class PauseWatcher:
    """Mirrors the host's pause switch (which the GUI flips with ``set_paused``) onto a script
    context. A daemon thread blocks in :func:`wait_pause` and hands every new state to
    ``apply``. Only in a GUI worker on an ABI 8 host; otherwise no thread is started. The
    script's own pause goes the other way, through :func:`set_paused`."""

    __slots__ = ("_apply", "_done", "_thread")

    def __init__(self, apply: Callable[[bool], None]) -> None:
        self._apply = apply
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "PauseWatcher":
        if self._thread is None and mode() == MODE_WORKER and _control_surface() is not None:
            self._thread = threading.Thread(target=self._watch, name="bwu-ctl-pause-watch",
                                            daemon=True)
            self._thread.start()
        return self

    def _watch(self) -> None:
        flag, seq = paused()
        self._apply(flag)
        while not self._done.is_set() and not stopping():
            new_seq, flag = wait_pause(PAUSE_WATCH_PERIOD_MS, seq)
            if new_seq != seq:
                seq = new_seq
                self._apply(flag)

    def close(self) -> None:
        self._done.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join()
        self._thread = None
