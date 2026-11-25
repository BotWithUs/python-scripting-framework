"""The script run log, over the native host's one sink (``botwithus._native.runlog_*``).

Every script run gets one redacted file under ``~/.botwithus/logs/scripts/<slug>/``. The
native host owns all of it that touches disk: the file, the header, the size caps and
retention, the redactor, and the breadcrumbs of the host calls a script makes. It also
installs, at interpreter boot, the ``sys.stdout``/``sys.stderr`` tee and the ``logging``
route that send a bound thread's output to its run. This module is the runner's side only:
open a run for a script on its own thread, record state changes and the crash block, close it.

Without the native run-log surface (the unit suite on a plain Python, an older host) every
call here is a no-op, so the framework behaves exactly as before.
"""
from __future__ import annotations

import inspect
import threading
import traceback
from typing import Any

from . import native as _native_seam

LOG_ERROR = 40


def _backend() -> Any:
    try:
        be = _native_seam.backend()
    except _native_seam.NativeUnavailable:
        return None
    return be if hasattr(be, "runlog_open") else None


def script_path(script: object) -> str | None:
    """The file a script's class was loaded from (hashed into the header), if any."""
    try:
        return inspect.getsourcefile(type(script))
    except (TypeError, OSError):
        return None


def describe(exc: BaseException) -> tuple[str, str, str]:
    """``(exception, top_frame, traceback)`` for a crash block.

    The native host's ``format_exception`` picks the innermost frame outside the stdlib,
    site-packages and this framework; without it the top frame is ``unknown``.
    """
    fmt = getattr(_backend(), "format_exception", None)
    if fmt is not None:
        return fmt(exc)
    te = traceback.TracebackException.from_exception(exc)
    return "".join(te.format_exception_only()).strip(), "unknown", "".join(te.format()).rstrip("\n")


class ScriptRun:
    """One script run's log. Open it on the thread the script runs on: that binds the thread,
    so the script's prints, ``logging`` records and host calls land in this run."""

    __slots__ = ("_be", "_id")

    def __init__(self, be: Any, run_id: int) -> None:
        self._be = be if run_id > 0 else None
        self._id = run_id

    @classmethod
    def open(cls, manifest: Any, script: object, pid: int = 0,
             path: str | None = None) -> "ScriptRun":
        """``path`` overrides the file found from ``script``'s class (for a run opened before
        there is a script object, such as a script file that failed to load)."""
        be = _backend()
        if be is None:
            return cls(None, 0)
        run_id = be.runlog_open(manifest.name, version=manifest.version, author=manifest.author,
                                path=path or script_path(script), pid=int(pid or 0))
        return cls(be, int(run_id))

    @property
    def active(self) -> bool:
        return self._be is not None

    def state(self, before: str, after: str) -> None:
        if self._be is not None:
            self._be.runlog_crumb(self._id, "state", f"{before}->{after}")

    def crash(self, phase: str, iteration: int, exc: BaseException) -> None:
        """Write the crash block. A run keeps only its first; a later failure (an ``on_stop``
        after an ``on_loop`` crash) is kept as an ERROR line instead."""
        if self._be is None:
            return
        exception, top_frame, trace = describe(exc)
        if not self._be.runlog_crash(self._id, phase, int(iteration), exception, top_frame, trace):
            self._be.runlog_write(self._id, LOG_ERROR, threading.current_thread().name,
                                  "botwithus.runtime", f"{phase} failed: {exception}", trace)

    def crash_text(self, phase: str, message: str, trace: str = "") -> None:
        """Write a crash block that has no exception object behind it (two plugins in one
        file, a failed attach). Like :meth:`crash`, only the run's first block is kept."""
        if self._be is None:
            return
        if not self._be.runlog_crash(self._id, phase, 0, message, "unknown", trace):
            self._be.runlog_write(self._id, LOG_ERROR, threading.current_thread().name,
                                  "botwithus.runtime", f"{phase} failed: {message}", trace)

    def close(self) -> None:
        if self._be is not None:
            self._be.runlog_close(self._id)
            self._be = None
