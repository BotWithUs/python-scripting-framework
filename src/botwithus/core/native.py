"""The one seam onto the native host (``botwithus._native``).

This framework runs **inside** ``bwu_host.exe`` (the native hybrid host), which embeds this
CPython and exposes ``botwithus._native`` as a builtin extension module. That module owns
the OS resources the old ctypes ``pipe``/``shm`` modules used to own -- the snapshot mapping
(with its magic + version validation), the RPC pipe, the event ring, and the SDN decrypt
seam. This layer no longer touches any of them directly; it asks the native host, which is
the CPython/Lua equivalent of the control point ``jvm.dll`` gives the Java host.

The seam is deliberately **raw**: the native host hands us the front-buffer bytes, the RPC
result bytes, and already-drained event records; the pure-Python decoders in ``snapshot``,
``events`` and ``msgpack_codec`` turn them into typed records. Snapshot/entity data is not a
secret (any consumer may read it), so decoding it in Python costs nothing the host cares
about -- what the host keeps is ownership of the resources and the decrypt path.

``_native`` is resolved once at import. Outside ``bwu_host`` (dev, tests) it is absent; the
suite injects a fake via :func:`set_backend`, mirroring how the Lua framework resolves the
global ``bwu`` at call time.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from contextlib import contextmanager

from dataclasses import dataclass
from collections.abc import Iterable, Iterator
from typing import Any

from . import layout as L
from . import msgpack_codec
from .rpc import RpcError
from .walk import (WALK_MAX_AVOID, Area, RouteLink, TransitionInfo, WalkEvent, WalkResult,
                   WalkTransition)

WW_STEP_KIND_WALK = 0
WW_STEP_KIND_TRANSITION = 1

# The largest value the host's walk_wait takes: it parses an unsigned 32-bit millisecond
# count (bwu_walk_wait's uint32_t, with "I", which wraps rather than refusing). The host
# reads this exact value as WAIT FOREVER (host_surface.cpp kWaitForever: an untimed wait
# until the walk ends), not as ~49.7 days. See clamp_wait_ms.
WALK_WAIT_MAX_MS = 0xFFFFFFFF
WALK_WAIT_FOREVER = WALK_WAIT_MAX_MS


def clamp_wait_ms(timeout_ms: int | float) -> int:
    """``timeout_ms`` as the host's walk_wait takes it: clamped to ``0..WALK_WAIT_MAX_MS``.

    Every value at or past the cap, ``float('inf')`` included, becomes
    :data:`WALK_WAIT_FOREVER`: the host then waits until the walk ends, however long that
    is. Clamped, not refused: a caller asking for a wait that long wants the walk's end, and
    that is what it gets. Sent unclamped, 2^32 ms would wrap to 0 and return at once.
    Negative values and ``-inf`` become 0 (no wait). NaN raises ``ValueError``."""
    if isinstance(timeout_ms, float) and math.isinf(timeout_ms):
        return WALK_WAIT_FOREVER if timeout_ms > 0 else 0
    return min(max(0, int(timeout_ms)), WALK_WAIT_MAX_MS)


@dataclass(frozen=True, slots=True)
class PathStep:
    """One planned step from ``bwu_path`` (worldwalker, linked in the native host)."""

    kind: int  # WW_STEP_KIND_WALK | WW_STEP_KIND_TRANSITION
    plane: int
    target_x: int
    target_y: int

    @property
    def is_walk(self) -> bool:
        return self.kind == WW_STEP_KIND_WALK


@dataclass(frozen=True, slots=True)
class Route:
    """One planned route: its steps and, in order, the transports and teleports it takes
    (``bwu_path_route``). ``links[i].step_index`` indexes ``steps``."""

    steps: tuple[PathStep, ...]
    links: tuple[RouteLink, ...]

    @property
    def teleports(self) -> tuple[RouteLink, ...]:
        """The links that are global teleports (lodestones, spells, items)."""
        return tuple(link for link in self.links if link.is_teleport)


class PathError(RuntimeError):
    """``bwu_path`` could not plan a route (no path, or worldwalker unavailable)."""


class WalkOptionsUnsupported(PathError):
    """The installed worldwalker.dll cannot honour the walk options asked for: it has no
    ``ww_executor_run_opts`` / ``ww_query_opts`` (so it cannot exclude transitions), no
    masked entry point (so it cannot apply ``disabled_moves``), or it predates WorldWalker #44
    (so it cannot avoid areas or list route links). Nothing was planned or walked.
    The fix is a newer worldwalker.dll; a recovery loop should stop excluding, not retry."""


# The native host's own refusal text for options the walker cannot honour (host_surface.cpp,
# worldwalker_link.cpp set_unsupported). Host-owned strings, so they are stable to match.
_OPTIONS_UNSUPPORTED = ("cannot exclude transitions", "cannot apply disabled_moves",
                        "cannot avoid areas", "cannot list route links")


def _steps(raw: Iterable[dict[str, Any]]) -> list[PathStep]:
    return [PathStep(int(s["kind"]), int(s["plane"]), int(s["x"]), int(s["y"])) for s in raw]


def _avoid_tuples(avoid: Iterable[Area] | None) -> list[tuple[int, int, int, int, int]]:
    """``avoid`` as the binding's tuples. Only :class:`Area` is taken, so a plane can never be
    read from the wrong position of a bare tuple."""
    if avoid is None:
        return []
    areas = list(avoid)
    for area in areas:
        if not isinstance(area, Area):
            raise TypeError(f"avoid takes walk.Area values, got {type(area).__name__}")
    if len(areas) > WALK_MAX_AVOID:
        raise ValueError(f"avoid: at most {WALK_MAX_AVOID} areas, got {len(areas)}")
    return [area.to_native() for area in areas]


def _path_error(msg: str) -> PathError:
    if any(s in msg for s in _OPTIONS_UNSUPPORTED):
        return WalkOptionsUnsupported(msg)
    return PathError(msg)

try:  # present only inside bwu_host.exe
    from botwithus import _native as _default_backend  # type: ignore
except Exception:  # pragma: no cover - exercised implicitly outside the host
    _default_backend = None

_backend: Any = _default_backend

# Substrings of the native host's last-error text that mean the transport is gone (see
# host_surface.cpp MapCall). A call failing with one of these marks the host dead rather
# than being reported as a per-call RPC error. "client exited" is what every RPC fails with
# once the client process has exited (refresh is then False too).
_CLIENT_EXITED = "client exited"
_TRANSPORT_DEAD = ("pipe disconnected", "agent pipe gone", "slots busy", _CLIENT_EXITED)


_log = logging.getLogger("botwithus.native")

# How long detach waits for host calls already in flight on other threads before it gives up
# and leaves the handle attached (a bounded leak) rather than free it under a live call.
DETACH_DRAIN_S = 2.0


class HostDetached(ConnectionError):
    """A call on a :class:`NativeHost` after :meth:`NativeHost.detach`. The handle is gone, so
    the call never reached the native host."""


class ClientExited(ConnectionError):
    """The call failed because the attached client process has exited. The host is marked
    dead. The worker and the CLI runner end a script that this escapes from as stopped, not
    crashed: the client going away is not the script's fault."""


class NativeUnavailable(RuntimeError):
    """``botwithus._native`` is not present and no fake backend was injected."""


class ProtocolMismatch(RuntimeError):
    """The native host speaks a different wire protocol than this package decodes.

    The host validates the agent's mapping against *its* vendored layout, then hands the raw
    bytes here to be decoded against :mod:`layout`. If the two versions differ, every offset
    past the first changed struct is wrong and decodes as plausible garbage, so attach
    refuses instead."""


def set_backend(backend: Any) -> None:
    """Install the native backend (the real ``_native`` module, or a test fake)."""
    global _backend
    _backend = backend


def backend() -> Any:
    if _backend is None:
        raise NativeUnavailable(
            "botwithus._native is unavailable -- run inside bwu_host.exe, "
            "or inject a fake with core.native.set_backend()"
        )
    return _backend


def discover_pids() -> list[int]:
    """Every attachable client pid the native host can see."""
    return sorted(int(p) for p in backend().discover_pids())


CLIENT_PID_ENV = "BWU_CLIENT_PID"


def default_client_pid(pid: int | None = None) -> int | None:
    """The client a script should attach to when it does not pick one itself.

    1. ``pid``, when given;
    2. ``BWU_CLIENT_PID``, but only in a GUI worker: that is the client the user chose in the
       GUI. ``bwu_host --py`` clears it, and it is ignored there in any case;
    3. the first client :func:`discover_pids` finds.

    None when there is no client to attach to."""
    if pid is not None:
        return int(pid)
    from . import ctl  # deferred: ctl imports this module

    if ctl.mode() == ctl.MODE_WORKER:
        env = os.environ.get(CLIENT_PID_ENV, "").strip()
        if env.isdigit() and int(env) > 0:
            return int(env)
    pids = discover_pids()
    return pids[0] if pids else None


class NativeHost:
    """A handle to one attached client, over the native host surface.

    Owns nothing itself -- the underlying resources live in ``bwu_host``; this is a thin,
    pid-scoped wrapper around the opaque host capsule the native module returns.
    """

    __slots__ = ("pid", "_be", "_h", "_alive", "_detached", "_calls", "_in_flight")

    def __init__(self, pid: int, handle: Any, be: Any) -> None:
        self.pid = pid
        self._be = be
        self._h = handle
        self._alive = True
        self._detached = False
        # Guards _detached and _in_flight: a call takes the handle only while not detached,
        # and detach frees it only once no call holds it.
        self._calls = threading.Condition()
        self._in_flight = 0

    @classmethod
    def attach(cls, pid: int) -> "NativeHost":
        be = backend()
        host_version = getattr(be, "PROTOCOL_VERSION", None)
        if host_version != L.PROTOCOL_VERSION:
            raise ProtocolMismatch(
                f"native host speaks protocol {host_version}, this package decodes "
                f"{L.PROTOCOL_VERSION}; update whichever side is behind"
            )
        handle = be.attach(int(pid))  # raises OSError on failure (no client / bad version)
        return cls(int(pid), handle, be)

    @property
    def alive(self) -> bool:
        return self._alive

    @contextmanager
    def _use(self) -> Iterator[Any]:
        """The handle for one native call. Raises :class:`HostDetached` after detach, so a
        freed handle is never passed down; while the call runs, detach waits for it."""
        with self._calls:
            if self._detached:
                raise HostDetached(f"the host for pid {self.pid} is detached")
            self._in_flight += 1
        try:
            yield self._h
        finally:
            with self._calls:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._calls.notify_all()

    def _transport_dead(self, msg: str) -> ConnectionError | None:
        """For a failed call's error text: None when the transport is still up, else mark the
        host dead and return the error to raise (:class:`ClientExited` after a client exit)."""
        if not any(s in msg for s in _TRANSPORT_DEAD):
            return None
        self._alive = False
        return ClientExited(msg) if _CLIENT_EXITED in msg else ConnectionError(msg)

    # --- snapshot ------------------------------------------------------------

    def refresh(self) -> bool:
        """``bwu_refresh``: take a fresh frame from the client. False when no frame could be
        taken -- once the client process has exited, every call is False ("client exited").
        The reads keep returning the last frame after that, so an ``on_stop`` can still log
        the final state; this is the one call that says the client is gone."""
        with self._use() as h:
            return bool(self._be.refresh(h))

    def snapshot_bytes(self) -> bytes:
        """A fresh, coherent copy of the current front snapshot buffer."""
        with self._use() as h:
            return self._be.snapshot_bytes(h)

    # --- rpc -----------------------------------------------------------------

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One request/response round trip; returns the agent's ``result`` value.

        The native host frames the request (assigning the id), waits for the matching
        reply, and skips any broker pushes in between. A handler error surfaces as
        :class:`RpcError`; a transport failure marks the host dead and raises
        :class:`ConnectionError` (:class:`ClientExited` once the client has exited).
        """
        req = msgpack_codec.pack({"method": method, "params": params or {}})
        try:
            with self._use() as h:
                resp = self._be.rpc_raw(h, req)
        except HostDetached:
            raise
        except Exception as exc:  # the binding raises RuntimeError(last_error) on failure
            msg = str(exc)
            if (dead := self._transport_dead(msg)) is not None:
                raise dead from exc
            raise RpcError(method, msg) from exc
        return msgpack_codec.unpack(resp)

    # --- varps / varbits -----------------------------------------------------

    def read_varps(self, ids: list[int]) -> list[dict[str, Any]]:
        """``bwu_read_varps``: one ``{id, state, value, value64, kind, default_verified}``
        record per id, in order. Decoded by :mod:`botwithus.core.variables`."""
        return self._var_read("read_varps", ids)

    def read_varbits(self, ids: list[int]) -> list[dict[str, Any]]:
        """``bwu_read_varbits``: the same record shape as :meth:`read_varps`."""
        return self._var_read("read_varbits", ids)

    def varp_defaults_status(self) -> int:
        """1 once varp defaults come from the cache, 0 while it warms up, -1 never."""
        fn = self._native_fn("varp_defaults_status")
        return int(fn())

    def constant(self, name: str) -> Any:
        """A constant the native module exports (``VARP_SET``...), or None if it has none."""
        return getattr(self._be, name, None)

    def _native_fn(self, name: str) -> Any:
        fn = getattr(self._be, name, None)
        if fn is None:
            raise NativeUnavailable(
                f"this bwu_host has no {name}; it predates varp state reads -- update it")
        return fn

    def _var_read(self, name: str, ids: list[int]) -> list[dict[str, Any]]:
        fn = self._native_fn(name)
        try:
            with self._use() as h:
                return fn(h, ids)
        except RuntimeError as exc:  # a failed read; TypeError / OverflowError are caller bugs
            msg = str(exc)
            if (dead := self._transport_dead(msg)) is not None:
                raise dead from exc
            raise RpcError(name, msg) from exc

    # --- plugin UI (ui_*) -------------------------------------------------------

    @property
    def has_ui(self) -> bool:
        """Whether this bwu_host has the plugin-UI surface (``ui_create`` and friends)."""
        return getattr(self._be, "ui_create", None) is not None

    @property
    def has_ui_p2(self) -> bool:
        """Whether this bwu_host has the plugin-UI P2 surface (hooks, blur, list-valued props).
        Detected by ``ui_subscribe``, as the native host says to, not by its ABI version."""
        return getattr(self._be, "ui_subscribe", None) is not None

    def ui_refused_type(self) -> type[BaseException] | None:
        """``botwithus._native.UiRefused``: the agent refused a ui call. None on an older host."""
        return getattr(self._be, "UiRefused", None)

    def ui(self, name: str, *args: Any) -> Any:
        """One ``botwithus._native.ui_<name>(host, *args)`` call.

        The binding's errors reach the caller as they are: ``UiRefused`` (the agent's refusal,
        its text verbatim), ``ValueError`` (the host refused without sending), ``TimeoutError``
        ("rpc_timeout": the outcome is unknown). A plain ``RuntimeError`` whose text says the
        transport is gone marks the host dead and becomes :class:`ConnectionError` /
        :class:`ClientExited`, as for every other call; any other one (a malformed reply)
        passes through unchanged."""
        fn = getattr(self._be, "ui_" + name, None)
        if fn is None:
            raise NativeUnavailable(
                f"this bwu_host has no ui_{name}; it predates the plugin UI -- update it")
        refused = self.ui_refused_type()
        try:
            with self._use() as h:
                return fn(h, *args)
        except RuntimeError as exc:
            if refused is not None and isinstance(exc, refused):
                raise
            if (dead := self._transport_dead(str(exc))) is not None:
                raise dead from exc
            raise

    # --- pathing -------------------------------------------------------------

    # Walk plan options, accepted by path / walk / walk_ex / walk_start (keyword-only):
    #   disabled_moves        WorldWalker movement categories switched off: a mask of
    #                         (1 << walk.MOVE_*) bits, optionally | walk.RESTRICT_FREE_TO_PLAY
    #   exclude               transition indices (WalkEvent / WalkTransition.transition_index)
    #                         the planner must not use, on the first plan and every re-plan
    #   exclude_loc_siblings  widen each excluded index to every row of the same loc
    # All at their defaults, the call is exactly the one made before options existed, so an
    # older bwu_host still works. Anything else needs a bwu_host with walk options
    # (:attr:`has_walk_options`; else :class:`NativeUnavailable`) and a worldwalker.dll that
    # can honour them (else :class:`WalkOptionsUnsupported`). Never silently ignored.

    @property
    def has_walk_options(self) -> bool:
        """Whether this bwu_host takes walk options (it has walk_options_supported)."""
        return getattr(self._be, "walk_options_supported", None) is not None

    def walk_options_supported(self) -> bool:
        """True when both this bwu_host and its worldwalker.dll can exclude transitions.
        False for an older bwu_host or an older worldwalker.dll. Raises :class:`PathError`
        when worldwalker cannot be loaded at all."""
        fn = getattr(self._be, "walk_options_supported", None)
        if fn is None:
            return False
        try:
            with self._use() as h:
                return bool(fn(h))
        except RuntimeError as exc:
            raise PathError(str(exc)) from exc

    @property
    def has_avoid(self) -> bool:
        """Whether this bwu_host takes avoid areas and plans routes with links (native ABI 7:
        it has path_route). The installed worldwalker.dll must know them too (WorldWalker #44),
        else a walk with areas raises :class:`WalkOptionsUnsupported`."""
        return getattr(self._be, "path_route", None) is not None

    def _walk_options(self, disabled_moves: int, exclude: Iterable[int] | None,
                      exclude_loc_siblings: bool, avoid: Iterable[Area] | None = None) -> dict[str, Any]:
        """The binding keywords for these options: {} at the defaults, so the call keeps its
        pre-options shape. Raises :class:`NativeUnavailable` for non-default options on a
        bwu_host that predates them, ``TypeError`` / ``ValueError`` for a malformed area."""
        opts: dict[str, Any] = {}
        if disabled_moves:
            opts["disabled_moves"] = int(disabled_moves)
        excluded = [int(i) for i in exclude] if exclude is not None else []
        if excluded:
            opts["exclude"] = excluded
        if exclude_loc_siblings:
            opts["exclude_loc_siblings"] = True
        areas = _avoid_tuples(avoid)
        if (opts or areas) and not self.has_walk_options:
            raise NativeUnavailable(
                "this bwu_host has no walk options (disabled_moves / exclude /"
                " exclude_loc_siblings); it predates them -- update it")
        if areas:
            if not self.has_avoid:
                raise NativeUnavailable("this bwu_host has no avoid areas; it predates them"
                                        " (native ABI 7) -- update it")
            opts["avoid"] = areas
        return opts

    def path(self, x: int, y: int, plane: int = 0, *, disabled_moves: int = 0,
             exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
             avoid: Iterable[Area] | None = None) -> list[PathStep]:
        """Plan a route from the player's LIVE tile to (x, y, plane) via the host's
        linked worldwalker. Returns the walk/transition steps (empty when already there);
        raises :class:`PathError` on no route or an unavailable pathfinder, and
        :class:`WalkOptionsUnsupported` for options the walker cannot honour. Options: see
        :attr:`has_walk_options`; ``avoid``: :attr:`has_avoid`."""
        opts = self._walk_options(disabled_moves, exclude, exclude_loc_siblings, avoid)
        raw = self._plan_call(self._be.path, x, y, plane, opts)
        return _steps(raw)

    def path_route(self, x: int, y: int, plane: int = 0, *, disabled_moves: int = 0,
                   exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
                   avoid: Iterable[Area] | None = None) -> Route:
        """Plan ONE route, as :meth:`path`, and return its steps with the transports and
        teleports it takes (:class:`Route`). Raises :class:`NativeUnavailable` on a bwu_host
        without route links (:attr:`has_avoid`), :class:`WalkOptionsUnsupported` on a
        worldwalker.dll without them (before WorldWalker #44), and :class:`PathError` as
        :meth:`path` does."""
        fn = getattr(self._be, "path_route", None)
        if fn is None:
            raise NativeUnavailable("this bwu_host has no path_route (route links); it predates"
                                    " them (native ABI 7) -- update it")
        opts = self._walk_options(disabled_moves, exclude, exclude_loc_siblings, avoid)
        raw = self._plan_call(fn, x, y, plane, opts)
        return Route(tuple(_steps(raw["steps"])),
                     tuple(RouteLink.from_native(link) for link in raw["links"]))

    def _plan_call(self, fn: Any, x: int, y: int, plane: int, opts: dict[str, Any]) -> Any:
        try:
            with self._use() as h:
                return fn(h, int(x), int(y), int(plane), **opts)
        except (TypeError, ValueError, HostDetached):
            raise   # a malformed option is the caller's bug, not a routing failure
        except Exception as exc:  # the binding raises RuntimeError(last_error) on failure
            msg = str(exc)
            if (dead := self._transport_dead(msg)) is not None:
                raise dead from exc
            raise _path_error(msg) from exc

    def walk(self, x: int, y: int, plane: int = 0, radius: int = 1, *, disabled_moves: int = 0,
             exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
             avoid: Iterable[Area] | None = None) -> bool:
        """BLOCKING: run the native transition executor to (x, y, plane) within ``radius``.

        Unlike :meth:`path` (plan only), this walks the whole route and *executes* its
        transitions (doors/stairs/teleports/dialogue/item chains), returning True on arrival.
        Blocks the calling thread for the walk; cancel it from another thread with
        :meth:`walk_cancel`. Options: see :attr:`has_walk_options`."""
        opts = self._walk_options(disabled_moves, exclude, exclude_loc_siblings, avoid)
        try:
            with self._use() as h:
                return bool(self._be.walk(h, int(x), int(y), int(plane), int(radius), **opts))
        except (TypeError, ValueError, HostDetached):
            raise
        except Exception as exc:
            msg = str(exc)
            if (dead := self._transport_dead(msg)) is not None:
                raise dead from exc
            raise _path_error(msg) from exc

    def walk_cancel(self) -> None:
        """Cancel the in-flight walk -- :meth:`walk`, :meth:`walk_ex` or :meth:`walk_start`
        (idempotent; from another thread for the blocking forms). A no-op after detach."""
        try:
            with self._use() as h:
                self._be.walk_cancel(h)
        except HostDetached:
            pass

    # --- walk progress (bwu_walk_*; feature-detected) --------------------------

    @property
    def has_walk_progress(self) -> bool:
        """Whether this bwu_host has the walk progress surface (walk_ex / walk_start / ...)."""
        return getattr(self._be, "walk_start", None) is not None

    def walk_ex(self, x: int, y: int, plane: int = 0, radius: int = 1, *,
                disabled_moves: int = 0, exclude: Iterable[int] | None = None,
                exclude_loc_siblings: bool = False, avoid: Iterable[Area] | None = None) -> WalkResult:
        """BLOCKING like :meth:`walk`, but returns a :class:`WalkResult`: status, the step and
        transition a failed walk failed on, the transition last attempted, re-plans used,
        elapsed time, and every walker event. On a host with the transition log
        (:attr:`has_walk_transitions`) it also carries ``transitions``: every transition the
        walk attempted, each with its ``baked`` record. Raises :class:`PathError` if a walk is
        already running on this host or the host refuses to start one (``not in game``),
        :class:`WalkOptionsUnsupported` for options the walker cannot honour. Options: see
        :attr:`has_walk_options`. An excluded index the walker refuses (out of range) is a
        FAILED result whose ``error`` says so."""
        fn = self._walk_fn("walk_ex")
        opts = self._walk_options(disabled_moves, exclude, exclude_loc_siblings, avoid)
        try:
            with self._use() as h:
                return WalkResult.from_native(
                    fn(h, int(x), int(y), int(plane), int(radius), **opts))
        except RuntimeError as exc:
            raise _path_error(str(exc)) from exc

    def walk_start(self, x: int, y: int, plane: int = 0, radius: int = 1, *,
                   disabled_moves: int = 0, exclude: Iterable[int] | None = None,
                   exclude_loc_siblings: bool = False, avoid: Iterable[Area] | None = None) -> None:
        """Start a walk and return at once; follow it with :meth:`walk_events` /
        :meth:`walk_wait`, stop it with :meth:`walk_cancel`. Raises :class:`PathError` if a
        walk is already running on this host or the host refuses to start one (``not in game``),
        :class:`WalkOptionsUnsupported` for options the walker cannot honour (the walk did not
        start). Options: see :attr:`has_walk_options`."""
        fn = self._walk_fn("walk_start")
        opts = self._walk_options(disabled_moves, exclude, exclude_loc_siblings, avoid)
        try:
            with self._use() as h:
                fn(h, int(x), int(y), int(plane), int(radius), **opts)
        except RuntimeError as exc:
            raise _path_error(str(exc)) from exc

    def walk_status(self) -> WalkResult | None:
        """The current or last walk's state, or None if this host has never walked."""
        fn = self._walk_fn("walk_status")
        with self._use() as h:
            d = fn(h)
        return None if d is None else WalkResult.from_native(d)

    def walk_wait(self, timeout_ms: int = 0) -> WalkResult | None:
        """Wait up to ``timeout_ms`` for the walk to end (the GIL is released), then its
        state; ``is_running`` is still True if it has not ended. None if never walked.

        ``timeout_ms`` is clamped to ``0..WALK_WAIT_MAX_MS`` (see :func:`clamp_wait_ms`): the
        host takes an unsigned 32-bit count, which would otherwise wrap a long wait to a short
        one. ``2**32 - 1`` and anything above it, ``float('inf')`` included, wait until the
        walk ends."""
        fn = self._walk_fn("walk_wait")
        with self._use() as h:
            d = fn(h, clamp_wait_ms(timeout_ms))
        return None if d is None else WalkResult.from_native(d)

    def walk_events(self, since: int = 0) -> list[WalkEvent]:
        """The current or last walk's events from index ``since`` on. Keep a cursor:
        ``since += len(returned)``."""
        fn = self._walk_fn("walk_events")
        with self._use() as h:
            raw = fn(h, max(0, int(since)))
        return [WalkEvent.from_native(e) for e in raw]

    # --- the walk's transition log + baked records (feature-detected) ---------

    @property
    def has_walk_transitions(self) -> bool:
        """Whether this bwu_host has walk_transitions / walk_describe (and so fills
        ``WalkResult.transitions`` and ``WalkTransition.baked`` from walk_ex)."""
        return (getattr(self._be, "walk_transitions", None) is not None
                and getattr(self._be, "walk_describe", None) is not None)

    def walk_transitions(self, since: int = 0) -> list[WalkTransition]:
        """Every transition the current or last walk attempted, from index ``since`` on, in
        order, each as observed and with its ``baked`` record. Empty if never walked. Keep a
        cursor (``since += len(returned)``) to follow a :meth:`walk_start` walk."""
        fn = self._walk_fn("walk_transitions", "the walk transition log")
        with self._use() as h:
            raw = fn(h, max(0, int(since)))
        return [WalkTransition.from_native(t) for t in raw]

    def walk_describe(self, transition_index: int) -> TransitionInfo | None:
        """The walker's baked record for ``transition_index`` (a WalkEvent / WalkTransition
        index), or None for no such transition (-1 included). Raises :class:`PathError` when
        the walker cannot describe at all (no worldwalker, or a worldwalker.dll without
        ww_transition_describe)."""
        fn = self._walk_fn("walk_describe", "the walk transition log")
        try:
            with self._use() as h:
                return TransitionInfo.from_native_or_none(fn(h, int(transition_index)))
        except RuntimeError as exc:
            raise PathError(str(exc)) from exc

    def _walk_fn(self, name: str, feature: str = "walk progress") -> Any:
        fn = getattr(self._be, name, None)
        if fn is None:
            raise NativeUnavailable(
                f"this bwu_host has no {name}; it predates {feature} -- update it")
        return fn

    # --- events --------------------------------------------------------------

    def poll_events_raw(self) -> list[dict[str, Any]]:
        """Drain the event ring once; a list of ``{"type": int, "body": bytes}`` records."""
        with self._use() as h:
            return self._be.poll_events(h)

    # --- lifecycle -----------------------------------------------------------

    @property
    def detached(self) -> bool:
        return self._detached

    def detach(self, drain_s: float | None = None) -> bool:
        """Release the host handle, once; every later call raises :class:`HostDetached`.
        Also after a transport failure marked the host dead (the client exited, say): the
        handle is still the host's to free.

        The native capsule keeps its pointer after the host frees it, so the handle is freed
        only once no call on another thread still holds it. Detach refuses new calls at once,
        cancels a walk in flight (it can block for minutes), then waits up to ``drain_s``
        (default :data:`DETACH_DRAIN_S`) for the calls to end. If they do not, it logs and
        leaves the handle attached: a bounded leak instead of a use after free. True when
        the handle is freed (by this call or an earlier one)."""
        with self._calls:
            self._alive = False
            if self._detached:
                return self._h is None
            self._detached = True
            busy = self._in_flight > 0
        if busy:
            self._cancel_walk_for_detach()
        limit = DETACH_DRAIN_S if drain_s is None else drain_s
        with self._calls:
            if not self._calls.wait_for(lambda: self._in_flight == 0, timeout=limit):
                _log.warning("pid %s: %d host call(s) still running after %.1f s; leaving the "
                             "handle attached rather than free it under them",
                             self.pid, self._in_flight, limit)
                return False
            handle, self._h = self._h, None
        try:
            self._be.detach(handle)
        except Exception:  # detach is best-effort; the process owns the resources
            pass
        return True

    def _cancel_walk_for_detach(self) -> None:
        """Unblock a blocking walk before detach waits for it. The handle is still valid
        here: it is freed only after the wait."""
        cancel = getattr(self._be, "walk_cancel", None)
        if cancel is None:
            return
        try:
            cancel(self._h)
        except Exception:  # best effort, like detach
            pass
