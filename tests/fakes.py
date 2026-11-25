"""In-memory fakes so the unit suite runs with no game client and no native host.

The framework now talks to ``botwithus._native``; the suite injects :class:`FakeNative`
in its place (via ``core.native.set_backend``). One fake stands in for the whole native
surface -- attach/detach, ``snapshot_bytes``, ``rpc_raw``, ``poll_events`` -- so the raw
seam is exercised end to end without a client.

- The GUI control surface (``ctl_*``, protocol section 9.2) is on every :class:`FakeNative`, in
  CLI mode unless ``ctl_mode=`` says otherwise; :meth:`FakeNative.gui_stop` and
  :meth:`FakeNative.gui_set_config` play the GUI's side. :class:`FakeRunlog` adds the run-log
  surface so a test can see the crash blocks a run writes.
- :func:`build_snapshot` / :func:`build_events` build the raw bytes/records the native host
  would hand back, so the pure-Python decoders (``snapshot``, ``events``) are tested against
  exactly the shapes they see in production.
"""
from __future__ import annotations

import struct
import threading
import time
from collections import deque

from botwithus.api.config import ConfigSchemaError, validate_describe
from botwithus.core import layout as L
from botwithus.core import msgpack_codec


def build_events(events: list[tuple[int, bytes]]) -> list[dict]:
    """Build the record list ``_native.poll_events`` returns: ``{"type", "body"}`` in order."""
    return [{"type": etype, "body": body} for etype, body in events]


def build_snapshot(*, game_state: int = L.STATE_IN_GAME, server_tick: int = 100,
                   game_cycle: int = 5000, publish_seq: int = 4999,
                   self_tile: tuple[int, int, int] = (3200, 3200, 0),
                   npc: tuple[int, int, tuple[int, int]] | None = None) -> bytes:
    """Build a minimal but valid snapshot buffer (the raw bytes ``snapshot_bytes`` returns)."""
    buf = bytearray(L.SNAPSHOT_SIZE)
    struct.pack_into("<Q", buf, L.SNAP_PUBLISH_SEQ, publish_seq)
    struct.pack_into("<i", buf, L.SNAP_GAME_STATE, game_state)
    struct.pack_into("<i", buf, L.SNAP_SERVER_TICK, server_tick)
    struct.pack_into("<i", buf, L.SNAP_GAME_CYCLE, game_cycle)
    # self
    base = L.SNAP_SELF
    struct.pack_into("<i", buf, base + L.LP_SERVER_INDEX, 1000)
    struct.pack_into("<i", buf, base + L.LP_COMBAT_LEVEL, 126)
    struct.pack_into("<h", buf, base + L.LP_TILE_X, self_tile[0])
    struct.pack_into("<h", buf, base + L.LP_TILE_Y, self_tile[1])
    struct.pack_into("<b", buf, base + L.LP_PLANE, self_tile[2])
    struct.pack_into("<I", buf, base + L.LP_SKILL_COUNT, 1)
    struct.pack_into("<i", buf, base + L.LP_SKILLS + L.SK_ACTUAL_LEVEL, 99)
    if npc is not None:
        server_index, type_id, (tx, ty) = npc
        struct.pack_into("<I", buf, L.SNAP_NPC_COUNT, 1)
        no = L.SNAP_NPCS
        struct.pack_into("<i", buf, no + L.NPC_SERVER_INDEX, server_index)
        struct.pack_into("<i", buf, no + L.NPC_TYPE_ID, type_id)
        struct.pack_into("<h", buf, no + L.NPC_TILE_X, tx)
        struct.pack_into("<h", buf, no + L.NPC_TILE_Y, ty)
    return bytes(buf)


class _FakeHostState:
    """Per-attach state a FakeNative capsule points at."""

    def __init__(self, pid, responder, snapshot, events):
        self.pid = pid
        self.responder = responder
        self.snapshot = snapshot
        self.events = list(events)  # drained once, like the real ring cursor


class FakeCtlError(RuntimeError):
    """``botwithus._native.CtlError``: a RuntimeError subclass."""


CTL_PHASES = ("attaching", "running", "stopping")


def _expect(ok: bool, path: str, what: str) -> None:
    if not ok:
        raise FakeCtlError(f"{path}: expected {what}")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_field_types(field, path: str) -> None:
    _expect(isinstance(field, dict), path, "a map")
    for key in ("key", "kind", "label", "description"):
        if field.get(key) is not None:
            _expect(isinstance(field[key], str), f"{path}.{key}", "a string")
    for key in ("min", "max", "step", "max_len"):
        if field.get(key) is not None:
            _expect(_is_int(field[key]), f"{path}.{key}", "an integer")
    default = field.get("default")
    kind = field.get("kind")
    if kind in ("int", "item_id"):
        _expect(_is_int(default), f"{path}.default", "an integer")
    elif kind in ("string", "choice"):
        _expect(isinstance(default, str), f"{path}.default", "a string")
    elif kind == "bool":
        _expect(isinstance(default, bool), f"{path}.default", "a bool")
    if field.get("options") is not None:
        opts = field["options"]
        _expect(isinstance(opts, (list, tuple)), f"{path}.options", "a sequence")
        for j, opt in enumerate(opts):
            _expect(isinstance(opt, str), f"{path}.options[{j}]", "a string")


def _check_desc_types(desc) -> None:
    """The binding's conversion of ``desc`` (protocol 9.2): read
    by key name; a wrong type fails with ``"<key path>: expected ..."`` as ``CtlError``."""
    manifest = desc.get("manifest")
    _expect(isinstance(manifest, dict), "manifest", "a map")
    for key in ("name", "version", "author", "description", "category"):
        if manifest.get(key) is not None:
            _expect(isinstance(manifest[key], str), f"manifest.{key}", "a string")
    schema = desc.get("config_schema")
    _expect(isinstance(schema, (list, tuple)), "config_schema", "a sequence")
    for i, field in enumerate(schema):
        _check_field_types(field, f"config_schema[{i}]")
    if desc.get("self_running") is not None:
        _expect(isinstance(desc["self_running"], bool), "self_running", "a bool")


class FakeNative:
    """A stand-in for the ``botwithus._native`` module.

    ``responder(method, params) -> result`` supplies RPC results; a responder returning the
    sentinel string ``"__error__:<msg>"`` makes ``rpc_raw`` fail the way the real binding
    does (RuntimeError with the host's last-error text).
    """

    # The real module's varp constants (bwu_host_surface.h BWU_VARP_* / BWU_VAR_KIND_*).
    VARP_UNAVAILABLE = 0
    VARP_DEFAULT_NOT_SET_CLIENTSIDE = 1
    VARP_SET = 2
    VARP_NO_SUCH_VARP = 3
    VAR_KIND_UNKNOWN = -1
    VAR_KIND_INT = 0
    VAR_KIND_LONG = 1
    VAR_KIND_STRING = 2
    VAR_NO_VALUE = -1

    # The ctl surface's constants and error class (bwu_host_surface.h BWU_CTL_MODE_*).
    CTL_CLI = 0
    CTL_WORKER = 1
    CTL_DESCRIBE = 2
    CtlError = FakeCtlError

    def __init__(self, *, pid: int = 4242, responder=None, snapshot: bytes | None = None,
                 events: list[dict] | None = None, path: list[dict] | None = None,
                 protocol_version: int = L.PROTOCOL_VERSION, var_reads=None,
                 defaults_status: int = 1, ctl_mode: int = 0,
                 ctl_saved_config: dict | None = None,
                 ctl_describe_error: str | None = None,
                 ctl_saved_settings: dict | None = None) -> None:
        # The real module exposes this as a constant; a fake built for a skew test overrides it.
        self.PROTOCOL_VERSION = protocol_version
        self._pid = pid
        self._responder = responder or (lambda m, p: {})
        self._snapshot = snapshot if snapshot is not None else build_snapshot()
        self._events = events or []
        self._path = path or []
        self.detached = False
        self.walk_result = True
        self.walk_calls: list[tuple] = []
        self.walk_cancelled = False
        self.walk_wait_calls: list[int] = []
        # var_reads(fn_name, ids) -> list of record dicts, or a "__error__:<msg>" string.
        self._var_reads = var_reads or (lambda fn, ids: [])
        self._defaults_status = defaults_status
        self.var_calls: list[tuple[str, list[int]]] = []
        self._init_ctl(ctl_mode, ctl_saved_config, ctl_describe_error)
        self._init_control(ctl_saved_settings)

    # --- ctl surface (protocol section 9.2) ----------------------------------------------
    #
    # Plays the host's half: ctl_describe returns the initial full set (defaults, plus the
    # GUI's saved values applied leniently in worker mode), ctl_state enforces the section 7.1
    # transitions, ctl_wait / ctl_stopping follow the stop flag, and ctl_take_config hands out
    # queued changes (the full set) only after "running" was accepted. ctl_log records every
    # call in order: ("describe", desc), ("state", phase), ("take", id), ("done", id, ok, msg),
    # plus the GUI-side ("gui_stop",) and ("refused", id) entries.

    def _init_ctl(self, mode, saved, describe_error) -> None:
        self._ctl_mode = mode
        self._ctl_saved = dict(saved) if saved else None
        self.ctl_describe_error = describe_error
        self.ctl_log: list[tuple] = []
        self._ctl_lock = threading.Lock()
        self._ctl_stop = threading.Event()
        self._ctl_described = False
        self._ctl_self_running = False
        self._ctl_phase: str | None = None
        self._ctl_running_accepted = False
        self._ctl_host_stopped = False
        self._ctl_committed: dict = {}
        self._ctl_pending: deque = deque()
        self._ctl_taken: dict[int, dict] = {}
        self.ctl_wait_calls = 0

    def ctl_mode(self):
        return self._ctl_mode

    def ctl_describe(self, desc):
        if not isinstance(desc, dict):
            raise TypeError("ctl_describe: desc must be a dict")
        with self._ctl_lock:
            if self._ctl_described:
                raise FakeCtlError("ctl_describe: called twice")
            self._ctl_described = True
            self.ctl_log.append(("describe", desc))
        _check_desc_types(desc)
        if self.ctl_describe_error is not None:
            raise FakeCtlError(self.ctl_describe_error)
        try:  # stands in for the host's ValidateDescribe (ctl/config.cpp)
            validate_describe(desc)
        except ConfigSchemaError as exc:
            raise FakeCtlError(str(exc)) from exc
        cfg = {f["key"]: f["default"] for f in desc["config_schema"]}
        if self._ctl_mode == self.CTL_WORKER and self._ctl_saved:
            for key, value in self._ctl_saved.items():  # Lenient: skip what does not fit
                if key in cfg and type(value) is type(cfg[key]):
                    cfg[key] = value
        self._ctl_self_running = bool(desc.get("self_running"))
        self._ctl_committed = dict(cfg)
        return dict(cfg)

    def _ctl_legal(self, phase) -> bool:
        cur = self._ctl_phase
        if cur is None:
            return phase == "attaching" or (phase == "running" and self._ctl_self_running)
        allowed = {"attaching": ("running", "stopping"), "running": ("stopping",)}
        return phase in allowed.get(cur, ())

    def ctl_state(self, phase):
        if phase not in CTL_PHASES:
            raise FakeCtlError(f"ctl_state: unknown phase {phase!r}")
        with self._ctl_lock:
            self.ctl_log.append(("state", phase))
            if self._ctl_mode != self.CTL_WORKER:
                return None
            if self._ctl_host_stopped:
                return None  # a no-op after a host-sent stopping
            if phase == "stopping" and self._ctl_phase == "stopping":
                return None
            if not self._ctl_legal(phase):
                raise FakeCtlError(f"ctl_state: illegal transition {self._ctl_phase} -> {phase}")
            self._ctl_phase = phase
            self._ctl_running_accepted |= phase == "running"
        return None

    def ctl_wait(self, timeout_ms):
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError("ctl_wait: timeout_ms must be an int")
        if not 0 <= timeout_ms <= 0xFFFFFFFF:
            raise ValueError("ctl_wait: timeout_ms out of range")
        self.ctl_wait_calls += 1
        if self._ctl_mode == self.CTL_CLI:
            time.sleep(timeout_ms / 1000.0)
            return "timeout"
        return "stop" if self._ctl_stop.wait(timeout_ms / 1000.0) else "timeout"

    def ctl_stopping(self):
        return self._ctl_mode != self.CTL_CLI and self._ctl_stop.is_set()

    def ctl_take_config(self):
        with self._ctl_lock:
            if (self._ctl_mode != self.CTL_WORKER or not self._ctl_running_accepted
                    or not self._ctl_pending):
                return None
            change_id, full = self._ctl_pending.popleft()
            self._ctl_taken[change_id] = full
            self.ctl_log.append(("take", change_id))
            return change_id, dict(full)

    def ctl_config_done(self, change_id, ok, message=""):
        with self._ctl_lock:
            self.ctl_log.append(("done", change_id, bool(ok), message))
            full = self._ctl_taken.pop(change_id, None)
            if ok and full is not None:
                self._ctl_committed = dict(full)

    # --- run control (native ABI 8, protocol 9.10) ----------------------------------------
    #
    # One pause switch with a sequence number, flipped by the script (ctl_set_paused) or the GUI
    # (gui_set_paused); ctl_log gets ("paused", flag, by) for each change. The settings store
    # answers ctl_save_settings with `save_result` and keeps every map in `saves`.

    CTL_SAVE_OK = 1
    CTL_SAVE_FAILED = 0
    CTL_SAVE_UNSUPPORTED = -1

    def _init_control(self, saved_settings) -> None:
        self._pause = threading.Condition()
        self._paused = False
        self._pause_seq = 0
        self._saved_settings = dict(saved_settings) if saved_settings else None
        self.saves: list[dict] = []
        self.save_result = (self.CTL_SAVE_OK, "")

    def _flip(self, flag: bool, by: str) -> bool:
        with self._pause:
            if self._ctl_mode != self.CTL_WORKER:
                return True
            if self._paused == flag:
                return True
            self._paused = flag
            self._pause_seq += 1
            self.ctl_log.append(("paused", flag, by))
            self._pause.notify_all()
            return True

    def ctl_paused(self):
        with self._pause:
            return self._paused, self._pause_seq

    def ctl_set_paused(self, flag):
        if not isinstance(flag, bool):
            raise TypeError("ctl_set_paused: paused must be a bool")
        return self._flip(flag, "script")

    def ctl_wait_pause(self, timeout_ms, since_seq):
        deadline = time.monotonic() + timeout_ms / 1000.0
        with self._pause:
            while self._pause_seq == since_seq and not self._ctl_stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._pause.wait(min(left, 0.02))
            return self._pause_seq, self._paused

    def ctl_settings(self):
        return None if self._ctl_mode != self.CTL_WORKER or self._saved_settings is None \
            else dict(self._saved_settings)

    def ctl_save_settings(self, values, timeout_ms=5000):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise TypeError("ctl_save_settings: keys and values must be str")
        if self._ctl_mode != self.CTL_WORKER:
            return self.CTL_SAVE_UNSUPPORTED, "save_settings: no settings store outside a GUI worker run"
        self.saves.append(dict(values))
        return self.save_result

    def gui_set_paused(self, flag: bool) -> None:
        """A ``set_paused`` frame arrived."""
        self._flip(flag, "control")

    # --- the GUI's side ------------------------------------------------------------------

    def gui_stop(self) -> None:
        """A ``stop`` frame arrived: the host sets the flag and sends ``state stopping``."""
        with self._ctl_lock:
            self.ctl_log.append(("gui_stop",))
            self._ctl_phase = "stopping"   # the host sends state stopping at once (7.4)
            self._ctl_host_stopped = True
        self._ctl_stop.set()

    def gui_set_config(self, change_id: int, values: dict) -> bool:
        """A valid ``set_config``: queued as the full set, or refused while stopping."""
        with self._ctl_lock:
            if self._ctl_stop.is_set():
                self.ctl_log.append(("refused", change_id))
                return False
            base = self._ctl_pending[-1][1] if self._ctl_pending else self._ctl_committed
            self._ctl_pending.append((change_id, {**base, **values}))
            return True

    @property
    def ctl_committed(self) -> dict:
        return dict(self._ctl_committed)

    def ctl_states(self) -> list[str]:
        return [e[1] for e in self.ctl_log if e[0] == "state"]

    # --- native surface ------------------------------------------------------

    def discover_pids(self):
        return [self._pid]

    def attach(self, pid):
        return _FakeHostState(pid, self._responder, self._snapshot, self._events)

    def detach(self, handle):
        self.detached = True

    def refresh(self, handle):
        return True

    def snapshot_bytes(self, handle):
        return handle.snapshot

    def rpc_raw(self, handle, req: bytes) -> bytes:
        call = msgpack_codec.unpack(req)
        result = handle.responder(call.get("method"), call.get("params"))
        if isinstance(result, str) and result.startswith("__error__:"):
            raise RuntimeError(result[len("__error__:"):])
        return msgpack_codec.pack(result)

    def poll_events(self, handle):
        drained, handle.events = handle.events, []  # one drain, then empty
        return drained

    def path(self, handle, x, y, plane):
        # A responder-style error sentinel makes bwu_path failures reproducible.
        if isinstance(self._path, str) and self._path.startswith("__error__:"):
            raise RuntimeError(self._path[len("__error__:"):])
        return [dict(s) for s in self._path]

    def walk(self, handle, x, y, plane, radius):
        # Records the request and returns a canned arrival result (set via walk_result).
        self.walk_calls.append((x, y, plane, radius))
        return self.walk_result

    # Walk progress: canned dicts in the exact shape the real binding returns (the host's
    # tests/bindings/walk_check.py asserts the same key sets). walk_result is the dict walk_ex
    # / walk_status / walk_wait return (None = never walked); walk_events_list the events.
    walk_progress = None
    walk_events_list: list = []
    # The transition log (walk_transitions / walk_ex "transitions"), the total walk_ex reports
    # as transitions_attempted (None = len of the log), the baked records walk_describe serves
    # by index, and an error walk_describe raises as RuntimeError (a walker that can't describe).
    walk_tx_log: list = []
    walk_tx_attempted = None
    walk_baked: dict = {}
    walk_describe_error = None
    walk_describe_calls: list = []

    def walk_ex(self, handle, x, y, plane, radius):
        self.walk_calls.append((x, y, plane, radius))
        if self.walk_progress is None:
            raise RuntimeError("a walk is already running on this host")
        attempted = (len(self.walk_tx_log) if self.walk_tx_attempted is None
                     else self.walk_tx_attempted)
        return dict(self.walk_progress, events=[dict(e) for e in self.walk_events_list],
                    transitions=[dict(t) for t in self.walk_tx_log],
                    transitions_attempted=attempted)

    def walk_transitions(self, handle, since=0):
        if since < 0:
            raise ValueError("walk_transitions: since must be >= 0")
        return [dict(t) for t in self.walk_tx_log[since:]]

    def walk_describe(self, handle, index):
        self.walk_describe_calls = [*self.walk_describe_calls, index]
        if self.walk_describe_error is not None:
            raise RuntimeError(self.walk_describe_error)
        rec = self.walk_baked.get(index)
        return None if rec is None else dict(rec)

    def walk_start(self, handle, x, y, plane, radius):
        self.walk_calls.append((x, y, plane, radius))
        if self.walk_progress is None:
            raise RuntimeError("a walk is already running on this host")

    def walk_status(self, handle):
        return None if self.walk_progress is None else dict(self.walk_progress)

    def walk_wait(self, handle, timeout_ms):
        self.walk_wait_calls.append(timeout_ms)
        return self.walk_status(handle)

    def walk_events(self, handle, since=0):
        return [dict(e) for e in self.walk_events_list[since:]]

    def walk_cancel(self, handle):
        self.walk_cancelled = True

    def _var_read(self, fn, ids):
        self.var_calls.append((fn, list(ids)))
        out = self._var_reads(fn, list(ids))
        if isinstance(out, str) and out.startswith("__error__:"):
            raise RuntimeError(out[len("__error__:"):])
        return out

    def read_varps(self, handle, ids):
        return self._var_read("read_varps", ids)

    def read_varbits(self, handle, ids):
        return self._var_read("read_varbits", ids)

    def varp_defaults_status(self):
        return self._defaults_status


def var_record(id, state, value, *, value64=None, kind=-1, verified=True):
    """One record in the shape _native.read_varps / read_varbits returns."""
    return {"id": id, "state": state, "value": value,
            "value64": value if value64 is None else value64,
            "kind": kind, "default_verified": verified}


def walk_transition(**over):
    """A BwuWalkTransition dict as the binding returns it (kind NONE by default)."""
    t = {"kind": 0, "transition_index": -1, "step_index": -1, "object_id": -1, "npc_type_hi": -1,
         "tile": {"x": 0, "y": 0, "plane": 0}, "option_index": -1, "interact_attempts": 0,
         "interact_issued": 0, "chain_count": 0, "chain": [], "baked": None}
    t.update(over)
    return t


def transition_info(**over):
    """A BwuTransitionInfo dict as the binding returns it (a baked LOC door by default)."""
    t = {"origin": 0, "kind": 0, "move_category": 0, "is_runtime_loaded": False,
         "origin_tile": {"x": 3205, "y": 3206, "plane": 0},
         "dest_tile": {"x": 3205, "y": 3207, "plane": 0},
         "object_id": 1530, "npc_type_min": -1, "npc_type_max": -1, "npc_search_radius": -1,
         "option_index": 1, "shape": 0, "rotation": 2, "code": "", "cost": 2.5,
         "requirement_count": 0, "chain_step_count": 0, "requirements": [], "chain": []}
    t.update(over)
    return t


def walk_result(**over):
    """A BwuWalkResult dict as the binding returns it (an arrival by default)."""
    r = {"status": 1, "result": 0, "final_event": 5, "fail_step": -1, "fail_transition": -1,
         "replans": 0, "event_count": 2, "events_dropped": 0, "elapsed_ms": 1200,
         "transition": walk_transition(), "error": ""}
    r.update(over)
    return r


def walk_event(kind, step=0, tx=-1, hint=0, t_ms=0, replans=0):
    return {"kind": kind, "step_index": step, "transition_index": tx,
            "interaction_hint": hint, "t_ms": t_ms, "replans": replans}


class FakeRunlog(FakeNative):
    """A :class:`FakeNative` with the run-log surface (``runlog_*``), recording what each run
    writes. Like the host, a run keeps only its first crash block."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.runlog_opens: list[dict] = []
        self.crashes: list[dict] = []
        self.runlog_lines: list[dict] = []
        self.runlog_closed: list[int] = []
        self._crashed_runs: set[int] = set()

    def runlog_open(self, name, version=None, author=None, path=None, pid=0):
        self.runlog_opens.append({"name": name, "version": version, "author": author,
                                  "path": path, "pid": pid})
        return len(self.runlog_opens)

    def runlog_crumb(self, run, kind, detail):
        pass

    def runlog_crash(self, run, phase, iteration, exception, top_frame, trace):
        if run in self._crashed_runs:
            return False
        self._crashed_runs.add(run)
        self.crashes.append({"run": run, "phase": phase, "iteration": iteration,
                             "exception": exception, "top_frame": top_frame, "trace": trace})
        return True

    def runlog_write(self, run, level, thread, logger, message, trace):
        self.runlog_lines.append({"run": run, "level": level, "message": message})

    def runlog_close(self, run):
        self.runlog_closed.append(run)
