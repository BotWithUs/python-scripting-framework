"""The entry points ``bwu_host`` calls to describe or run one script for the GUI.

``bwu_host --describe ... --py <file>`` calls :func:`describe`, and ``bwu_host --worker ...
--py <file>`` calls :func:`run`, on the interpreter's main thread with ``sys.argv == [path]``
(``docs/GUI-WORKER-PROTOCOL.md`` sections 9.3, 9.4 and 9.6, native-scripting-host). Both
return only when the script is finished. Their outcome is not a return value: a failure is a
crash block in the run log, which the host turns into the GUI's ``crash`` message.

**How a file is read.** The file is imported as the module ``bwu_script``, with its directory
first on ``sys.path``. Exactly one concrete ``BotScript`` subclass defined in it is the plugin;
its ``@script_manifest`` and ``get_config_fields()`` describe it. A ``BotScript`` subclass
defined in the file but left abstract (no ``on_loop``), with no concrete one beside it, is a
load failure. A file that defines no ``BotScript`` is *self-running*: the GUI shows it with its
file name and no settings, and a run executes it again as ``__main__``, the way
``bwu_host --py`` does. Its self-run must sit under ``if __name__ == "__main__":``, or it
already ran once during the import.

**A plugin run, in order.** Describe; report ``attaching`` and attach to the client the GUI
chose; ``on_config_update`` with the initial settings; ``on_start``; report ``running``; loop
(apply queued setting changes, ``on_loop``, stop-aware sleep) until stop, a negative delay,
the client exiting or a crash; report ``stopping``; ``on_stop``; detach.

**The run log.** Each entry opens one run at the start, on the main thread; in a worker that
is the host's own run, adopted. Every crash block this module writes goes to that run.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import math
import os
import runpy
import sys
import threading
from dataclasses import dataclass
from typing import Any

from .api.config import ConfigSchemaError, schema_to_wire
from .api.script import BotScript, ScriptContext, ScriptManifest, manifest_of
from .api.timing import bound_context
from .core import ctl
from .core.connection import Connection
from .core.native import ClientExited
from .core.runlog import ScriptRun, describe as _describe_exc

_log = logging.getLogger("botwithus.worker")

MODULE_NAME = "bwu_script"

PHASE_LOAD = "load"
PHASE_CONFIG = "on_config_update"
PHASE_START = "on_start"
PHASE_LOOP = "on_loop"
PHASE_STOP = "on_stop"
PHASE_OTHER = "other"


@dataclass(frozen=True, slots=True)
class _Loaded:
    path: str
    manifest: ScriptManifest
    plugin: BotScript | None  # None: the file is self-running
    desc: dict[str, Any]


class _LoadError(Exception):
    """A load failure with a message but no script exception behind it."""


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _file_manifest(path: str) -> ScriptManifest:
    """A self-running file's manifest: its file name and the defaults (section 7.2)."""
    return ScriptManifest(name=_stem(path))


def _open_run(path: str, pid: int) -> ScriptRun:
    """The entry's one run, opened on the calling (main) thread: in a worker, the host's."""
    return ScriptRun.open(_file_manifest(path), None, pid, path=path)


def _is_clean_exit(exc: BaseException) -> bool:
    return isinstance(exc, SystemExit) and (exc.code is None or exc.code == 0)


# --- loading -------------------------------------------------------------------------------

def _import_script(path: str) -> Any:
    directory = os.path.dirname(os.path.abspath(path))
    if not sys.path or sys.path[0] != directory:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path!r} as a Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _missing(cls: type) -> str:
    missing = sorted(getattr(cls, "__abstractmethods__", ()))
    if missing == ["on_loop"]:
        return f"{cls.__name__}: on_loop is missing"
    return f"{cls.__name__}: not implemented: {', '.join(missing)}"


def _find_plugin(module: Any, path: str) -> type | None:
    """The one concrete ``BotScript`` defined in ``module``, or None for a self-running file.
    Raises :class:`_LoadError` for two or more, or for only abstract ones."""
    defined = [obj for obj in vars(module).values()
               if inspect.isclass(obj) and issubclass(obj, BotScript) and obj is not BotScript
               and obj.__module__ == MODULE_NAME]
    concrete = [c for c in defined if not inspect.isabstract(c)]
    if len(concrete) > 1:
        names = ", ".join(sorted(c.__name__ for c in concrete))
        raise _LoadError(f"more than one BotScript in {os.path.basename(path)}: {names}")
    if concrete:
        return concrete[0]
    if defined:
        raise _LoadError("; ".join(_missing(c) for c in sorted(defined, key=lambda c: c.__name__)))
    return None


def _load(path: str, run: ScriptRun) -> _Loaded | None:
    """Plugin-mode load (section 9.4). None after writing a ``load`` crash block."""
    try:
        module = _import_script(path)
        cls = _find_plugin(module, path)
        if cls is None:
            desc = {"manifest": _file_manifest(path).to_wire(), "config_schema": [],
                    "self_running": True}
            return _Loaded(path, _file_manifest(path), None, desc)
        manifest = manifest_of(cls)
        plugin = cls()
        desc = {"manifest": manifest.to_wire(),
                "config_schema": schema_to_wire(plugin.get_config_fields())}
        return _Loaded(path, manifest, plugin, desc)
    except _LoadError as exc:
        run.crash_text(PHASE_LOAD, str(exc))
    except ConfigSchemaError as exc:
        run.crash_text(PHASE_LOAD, f"ConfigSchemaError: {exc}", _describe_exc(exc)[2])
    except BaseException as exc:  # noqa: BLE001 - SystemExit / KeyboardInterrupt at import too
        run.crash(PHASE_LOAD, 0, exc)
    return None


# --- describe ------------------------------------------------------------------------------

def describe(path: str) -> None:
    """Load ``path`` and report its description to the GUI. Never attaches, never calls a
    script callback."""
    run = _open_run(path, 0)
    try:
        loaded = _load(path, run)
        if loaded is None:
            return
        try:
            ctl.describe(loaded.desc)
        except ctl.CtlError:
            return  # the host has the reason and reports crash{schema}
    finally:
        run.close()


# --- run -----------------------------------------------------------------------------------

def run(path: str, client_pid: int) -> None:
    """Describe ``path``, then run it against the client ``client_pid`` until it stops."""
    entry_run = _open_run(path, client_pid)
    try:
        loaded = _load(path, entry_run)
        if loaded is None:
            return
        try:
            config = ctl.describe(loaded.desc)
        except ctl.CtlError:
            return
        if ctl.stopping():
            return
        if loaded.plugin is None:
            _report("running")
            _self_run(path, entry_run)
            return
        _run_plugin(loaded.plugin, loaded.manifest.name, client_pid, config, entry_run)
    finally:
        entry_run.close()


def _report(phase: str) -> None:
    """``ctl_state(phase)``, tolerating the refusal a host-sent stop can cause.

    Once the host has sent ``stopping`` (a GUI stop arrived during attach, ``on_config_update``
    or ``on_start``), reporting ``attaching`` / ``running`` / ``stopping`` is a no-op for the
    host. A host that still refuses it is treated the same
    way: the stop is real, and the run goes on to ``on_stop`` and detach."""
    try:
        ctl.state(phase)
    except ctl.CtlError:
        if not ctl.stopping():
            raise


def _self_run(path: str, run: ScriptRun) -> None:
    """Run a self-running file the CLI way, as ``__main__`` (section 7.2)."""
    try:
        runpy.run_path(path, run_name="__main__")
    except BaseException as exc:  # noqa: BLE001 - SystemExit and KeyboardInterrupt too
        if not _is_clean_exit(exc):
            run.crash(PHASE_LOOP, 0, exc)


def _attach_failure(exc: BaseException) -> str:
    """``attach failed: <code>: <text>``. Python has no attach status code, so the code is
    the OS error number when there is one, else the exception's class name."""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code is None:
        return f"attach failed: {type(exc).__name__}: {exc}"
    text = getattr(exc, "strerror", None) or str(exc)
    return f"attach failed: {code}: {text}"


def _run_plugin(plugin: BotScript, name: str, pid: int, config: dict[str, Any],
                run: ScriptRun) -> None:
    _report("attaching")
    if ctl.stopping():
        return
    try:
        conn = Connection(pid)
    except Exception as exc:
        run.crash_text(PHASE_OTHER, _attach_failure(exc), _describe_exc(exc)[2])
        return
    try:
        _drive(plugin, conn, conn.new_context(name, threading.Event()), config, run)
    finally:
        conn.close()


def _callback(run: ScriptRun, phase: str, fn: Any, *args: Any) -> bool:
    """Call one script callback. False after writing ``phase``'s crash block: any exception,
    ``SystemExit`` and ``KeyboardInterrupt`` included. False with no crash block when the
    callback failed because the client exited (:class:`ClientExited`)."""
    try:
        fn(*args)
        return True
    except ClientExited as exc:
        _log.info("client exited during %s: %s", phase, exc)
        return False
    except BaseException as exc:  # noqa: BLE001
        run.crash(phase, 0, exc)
        return False


def _drive(plugin: BotScript, conn: Connection, ctx: ScriptContext, config: dict[str, Any],
           run: ScriptRun) -> None:
    """Section 9.6 steps 5 to 11, on this (the main) thread, with ``ctx`` bound as the thread's
    script context (the condition waits in :mod:`botwithus.api.timing` find it there)."""
    with bound_context(ctx):
        _drive_bound(plugin, conn, ctx, config, run)


def _drive_bound(plugin: BotScript, conn: Connection, ctx: ScriptContext,
                 config: dict[str, Any], run: ScriptRun) -> None:
    watcher = ctl.StopWatcher(ctx._stop).start()
    pauses = _bind_run_control(ctx)
    try:
        if ctx.is_stop_requested():
            return  # on_start never ran, so neither does on_stop
        if not _callback(run, PHASE_CONFIG, plugin.on_config_update, ctx._set_config(config)):
            return
        if not _callback(run, PHASE_START, plugin.on_start, ctx):
            ctx._close_ui_delivery()   # no ui handler runs during or after on_stop
            _callback(run, PHASE_STOP, plugin.on_stop)
            return
        _report("running")
        _loop(plugin, conn, ctx, run)
        _report("stopping")
        ctx._close_ui_delivery()   # no ui handler runs during or after on_stop
        _callback(run, PHASE_STOP, plugin.on_stop)  # a second crash is kept as an ERROR line
    finally:
        ctx._release_ui()  # after on_stop; the host's release at detach is the backstop
        pauses.close()
        ctx._bind_pause_sink(None)
        watcher.close()


def _bind_run_control(ctx: ScriptContext) -> ctl.PauseWatcher:
    """ABI 8 run control for this run (GUI-WORKER-PROTOCOL.md 9.10): the saved settings seed
    ``ctx.settings`` (the config is merged over them next), ``save()`` goes to the GUI's store,
    the script's pauses go to the host, and the GUI's come back through a watcher thread."""
    saved = ctl.saved_settings()
    if saved:
        ctx.settings._merge(saved)
    ctx.settings._bind_store(lambda values: _save_settings(ctx, values))
    ctx._bind_pause_sink(ctl.set_paused)
    return ctl.PauseWatcher(ctx._apply_pause).start()


def _save_settings(ctx: ScriptContext, values: dict[str, str]) -> bool:
    code, why = ctl.save_settings(values)
    if code != ctl.SAVE_OK:
        ctx.log.warn(f"settings not saved: {why}")
    return code == ctl.SAVE_OK


def _is_delay(value: Any) -> bool:
    """A number the loop sleeps for: non-negative, ``inf`` included (a stop-aware sleep that
    only a stop ends). NaN, a bool or a non-number is not one."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and not math.isnan(value) and value >= 0)


def _client_gone(ctx: ScriptContext) -> bool:
    """The attached client has exited, by the rule in :mod:`botwithus.core.client_exit` (the
    same watch the context's sleeps use). The confirm wait ends early on a stop; the loop then
    sees the stop and ends as stopped."""
    watch = ctx._exit_watch
    return watch is not None and watch.confirm(ctx._stop)


def _loop(plugin: BotScript, conn: Connection, ctx: ScriptContext, run: ScriptRun) -> None:
    iteration = 0
    while not ctx.is_stop_requested():
        if not _apply_queued_config(plugin, ctx, run):
            return
        if ctx.is_stop_requested():
            return
        if _client_gone(ctx):
            _log.info("client %s exited; stopping", conn.pid)
            return
        if not ctx._await_resumed():
            return  # stopped (or the client exited) while paused
        iteration += 1
        try:
            delay = plugin.on_loop()
        except ClientExited as exc:
            _log.info("client %s exited during on_loop; stopping: %s", conn.pid, exc)
            return
        except BaseException as exc:  # noqa: BLE001
            if not _is_clean_exit(exc):
                run.crash(PHASE_LOOP, iteration, exc)
            return
        if not _is_delay(delay):
            return
        if not ctx.sleep(delay):
            return


def _apply_queued_config(plugin: BotScript, ctx: ScriptContext, run: ScriptRun) -> bool:
    """Apply every queued settings change in order. False when ``on_config_update`` raised:
    the change is refused and the run ends, as a crash, or as stopped when the raise was
    :class:`ClientExited`."""
    while (taken := ctl.take_config()) is not None:
        change_id, values = taken
        try:
            plugin.on_config_update(ctx._set_config(values))
        except ClientExited as exc:
            # Not applied, and not a crash: the run ends stopped, through on_stop.
            _log.info("client exited during %s: %s", PHASE_CONFIG, exc)
            ctl.config_done(change_id, False, f"client exited: {exc}")
            return False
        except BaseException as exc:  # noqa: BLE001
            ctl.config_done(change_id, False, f"{type(exc).__name__}: {exc}")
            run.crash(PHASE_CONFIG, 0, exc)
            return False
        ctl.config_done(change_id, True)
    return True
