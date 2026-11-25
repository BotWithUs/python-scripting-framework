"""Debug drawing: overlay commands the agent renders over the game client.

Build a command with a module function, then send it through ``game.draw``::

    from botwithus.api import draw

    game.draw.set(draw.rect("target", 100, 100, 40, 40, style=draw.Style(color=draw.RED)))
    game.draw.highlight(draw.highlight_npc(npc.server_index, label="target"))
    game.draw.set_batch([draw.line("a", 0, 0, 50, 50), draw.text("b", 10, 10, "hello")])

Three properties shape all of it (they mirror the Java host's ``DrawAPI``):

* **Keyed, not handled.** A key is 1..47 UTF-8 bytes. Setting the same key again replaces the
  command, so redrawing every tick is idempotent and there is nothing to free.
* **Owned by the pipe connection, not by the script.** The agent scopes keys,
  :meth:`DebugDraw.clear_all` and :meth:`DebugDraw.list` to the pipe connection, and drops
  everything a connection drew when it closes. One ``Connection`` has one pipe, and several
  scripts can run on one ``Connection``: those scripts share one key space, and ``clear_all``
  removes all of their drawings. Prefix your keys with the script's name if that matters.
  Unlike plugin UI components, the framework does not clear a script's drawings when it stops.
* **TTL is always sent.** Every command carries ``ttl_ms`` (default :data:`DEFAULT_TTL_MS`),
  so a stopped script's drawings fade on their own. :data:`TTL_UNTIL_CLEARED` (0) keeps a
  command until it is replaced, cleared or the connection closes, which can outlive the script.

Primitives (line, rect, ellipse, text, poly, component box) ride ``debug_draw_set`` and can be
batched. Highlights (an NPC, a player, yourself, a tile, an area, a component) ride their own
``highlight_*`` call each: the agent re-reads the target every tick, so a highlight follows a
walking NPC where a world rect at its tile would not. A highlight cannot go in a batch.

Units: screen geometry is client pixels. World primitives (``world=True``) take **sub-tiles**,
:data:`SUBTILE_SCALE` per tile; the ``highlight_*`` functions take whole **tiles**.

Each builder checks what the agent would refuse, so a mistake raises :class:`ValueError` at the
call site rather than coming back as an error string or a batch ``dropped`` count. The agent's
own refusals (a full store, an exhausted text or poly slot) raise
:class:`~botwithus.core.rpc.RpcError`. A world point that projects off screen is not a refusal:
it is accepted and not drawn (:meth:`DebugDraw.list` shows its ``project`` state).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ..core.rpc import MalformedReply

if TYPE_CHECKING:
    from ..core.rpc import RpcClient

# The agent's limits (NXTLibrary src/overlay/DrawTypes.h, kDraw* in src/rpc/Handlers.cpp).
MAX_KEY_BYTES = 47
MAX_TEXT_UTF16_UNITS = 127
MAX_BATCH_ITEMS = 256
MAX_COMMANDS = 512
MIN_POLY_POINTS = 2
MAX_POLY_POINTS = 32
MIN_THICKNESS = 1
MAX_THICKNESS = 64
MIN_Z = -0x8000
MAX_Z = 0x7FFF
MIN_PLANE = 0
MAX_PLANE = 3
MAX_COLOR = 0xFFFFFFFF
MAX_SCREEN_COORDINATE = 1 << 20
MAX_WORLD_COORDINATE = 1 << 23
MAX_EXTENT = 1 << 14
SUBTILE_SCALE = 256
MAX_WORLD_TILE = MAX_WORLD_COORDINATE // SUBTILE_SCALE
LIST_PAGE = 128

DEFAULT_TTL_MS = 3000
TTL_UNTIL_CLEARED = 0

# Opaque 0xAARRGGBB colours. Not the plugin UI's order: botwithus.api.ui takes 0xRRGGBBAA.
GREEN = 0xFF00FF00
RED = 0xFFFF0000
BLUE = 0xFF0000FF
CYAN = 0xFF00FFFF
MAGENTA = 0xFFFF00FF
YELLOW = 0xFFFFFF00
WHITE = 0xFFFFFFFF

FONTS = ("normal", "small", "large", "heading")
SCREEN = "screen"
WORLD = "world"


def argb(r: int, g: int, b: int, a: int = 0xFF) -> int:
    """A packed ``0xAARRGGBB`` colour from 0..255 channels."""
    for name, value in (("r", r), ("g", g), ("b", b), ("a", a)):
        if not _is_int(value) or not 0 <= value <= 0xFF:
            raise ValueError(f"{name} must be 0..255, got {value!r}")
    return (a << 24) | (r << 16) | (g << 8) | b


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(value: Any, low: int, high: int, name: str) -> int:
    if not _is_int(value) or not low <= value <= high:
        raise ValueError(f"{name} must be an int in {low}..{high}, got {value!r}")
    return value


def _key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("a draw key must be a non-empty string")
    size = len(key.encode("utf-8"))
    if size > MAX_KEY_BYTES:
        raise ValueError(f"draw key is {size} UTF-8 bytes, the agent takes at most "
                         f"{MAX_KEY_BYTES}: {key!r}")
    return key


def _caption(text: str, name: str) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be a string, got {text!r}")
    units = len(text.encode("utf-16-le")) // 2
    if units > MAX_TEXT_UTF16_UNITS:
        raise ValueError(f"{name} is {units} UTF-16 units, the agent takes at most "
                         f"{MAX_TEXT_UTF16_UNITS}")
    return text


@dataclass(frozen=True, slots=True)
class Style:
    """Colour (``0xAARRGGBB``), stroke, fill, paint order and lifetime of one command.

    ``filled`` applies to rects and ellipses; ``closed`` to a poly (join the last point to the
    first). ``z`` orders painting, higher on top."""
    color: int = GREEN
    thickness: int = MIN_THICKNESS
    filled: bool = False
    closed: bool = False
    z: int = 0
    ttl_ms: int = DEFAULT_TTL_MS

    def __post_init__(self) -> None:
        _int_in(self.color, 0, MAX_COLOR, "color")
        _int_in(self.thickness, MIN_THICKNESS, MAX_THICKNESS, "thickness")
        _int_in(self.z, MIN_Z, MAX_Z, "z")
        if not _is_int(self.ttl_ms) or self.ttl_ms < 0:
            raise ValueError(f"ttl_ms must be a non-negative int, got {self.ttl_ms!r}")

    def wire(self) -> dict[str, Any]:
        return {"color": self.color, "thickness": self.thickness, "filled": bool(self.filled),
                "closed": bool(self.closed), "z": self.z, "ttl_ms": self.ttl_ms}


DEFAULT_STYLE = Style()


@dataclass(frozen=True, slots=True)
class DrawCommand:
    """A primitive for ``debug_draw_set`` / ``debug_draw_set_batch``. Make one with
    :func:`line`, :func:`rect`, :func:`ellipse`, :func:`text`, :func:`poly` or
    :func:`component_box`; :meth:`wire` is the exact map sent."""
    key: str
    kind: str
    space: str
    geometry: Mapping[str, Any]
    style: Style = DEFAULT_STYLE

    def wire(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "space": self.space,
                **self.style.wire(), **self.geometry}


@dataclass(frozen=True, slots=True)
class Highlight:
    """A highlight for one ``highlight_*`` call. ``key`` None lets the agent name it, as
    ``npc:<i>``, ``player:<i>``, ``self``, ``tile:<x>:<y>:<plane>``,
    ``area:<x>:<y>:<w>:<h>:<plane>`` or ``comp:<iface>:<comp>``: the key
    :meth:`DebugDraw.highlight` returns, to clear it by."""
    method: str
    target: Mapping[str, Any]
    key: str | None = None
    label: str | None = None
    style: Style = DEFAULT_STYLE

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {**self.style.wire(), **self.target}
        if self.key is not None:
            out["key"] = self.key
        if self.label:
            out["label"] = self.label
        return out


def _space(world: bool, plane: int | None) -> tuple[str, dict[str, Any]]:
    if not world:
        if plane is not None:
            raise ValueError("only world space takes a plane")
        return SCREEN, {}
    if plane is None:
        return WORLD, {}
    return WORLD, {"plane": _int_in(plane, MIN_PLANE, MAX_PLANE, "plane")}


def _coord(value: int, world: bool, name: str) -> int:
    bound = MAX_WORLD_COORDINATE if world else MAX_SCREEN_COORDINATE
    return _int_in(value, -bound, bound, name)


def _primitive(key: str, kind: str, world: bool, plane: int | None,
               geometry: dict[str, Any], style: Style) -> DrawCommand:
    space, extra = _space(world, plane)
    return DrawCommand(_key(key), kind, space, MappingProxyType({**geometry, **extra}), style)


def line(key: str, x1: int, y1: int, x2: int, y2: int, *, world: bool = False,
         plane: int | None = None, style: Style = DEFAULT_STYLE) -> DrawCommand:
    """A straight segment. ``world=True`` takes sub-tile coordinates and an optional plane."""
    geometry = {name: _coord(v, world, name)
                for name, v in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2))}
    return _primitive(key, "line", world, plane, geometry, style)


def _box(kind: str, key: str, x: int, y: int, w: int, h: int, world: bool,
         plane: int | None, style: Style) -> DrawCommand:
    geometry = {"x": _coord(x, world, "x"), "y": _coord(y, world, "y"),
                "w": _int_in(w, 1, MAX_EXTENT, "w"), "h": _int_in(h, 1, MAX_EXTENT, "h")}
    return _primitive(key, kind, world, plane, geometry, style)


def rect(key: str, x: int, y: int, w: int, h: int, *, world: bool = False,
         plane: int | None = None, style: Style = DEFAULT_STYLE) -> DrawCommand:
    """An axis-aligned rectangle, stroked or (``style.filled``) filled."""
    return _box("rect", key, x, y, w, h, world, plane, style)


def ellipse(key: str, x: int, y: int, w: int, h: int, *, world: bool = False,
            plane: int | None = None, style: Style = DEFAULT_STYLE) -> DrawCommand:
    """The ellipse inscribed in the rectangle ``x, y, w, h``."""
    return _box("ellipse", key, x, y, w, h, world, plane, style)


def text(key: str, x: int, y: int, caption: str, *, font: str | None = None,
         world: bool = False, plane: int | None = None,
         style: Style = DEFAULT_STYLE) -> DrawCommand:
    """``caption`` anchored at ``x, y``. ``font`` is one of :data:`FONTS`."""
    if not _caption(caption, "caption"):
        raise ValueError("a text command needs a non-empty caption")
    geometry: dict[str, Any] = {"x": _coord(x, world, "x"), "y": _coord(y, world, "y"),
                                "text": caption}
    if font is not None:
        if font not in FONTS:
            raise ValueError(f"font must be one of {FONTS}, got {font!r}")
        geometry["font"] = font
    return _primitive(key, "text", world, plane, geometry, style)


def poly(key: str, points: Sequence[tuple[int, int]], *,
         style: Style = DEFAULT_STYLE) -> DrawCommand:
    """A polyline through 2..32 screen points; ``style.closed`` joins the ends. Screen only:
    the agent refuses a world poly (send lines instead)."""
    pairs = list(points)
    if not MIN_POLY_POINTS <= len(pairs) <= MAX_POLY_POINTS:
        raise ValueError(f"poly needs {MIN_POLY_POINTS}..{MAX_POLY_POINTS} points, "
                         f"got {len(pairs)}")
    flat: list[int] = []
    for i, pair in enumerate(pairs):
        if len(pair) != 2:
            raise ValueError(f"point {i} must be an (x, y) pair, got {pair!r}")
        flat += [_coord(pair[0], False, f"points[{i}].x"), _coord(pair[1], False, f"points[{i}].y")]
    return _primitive(key, "poly", False, None, {"points": flat}, style)


def component_box(key: str, iface: int, comp: int, *, label: str | None = None,
                  style: Style = DEFAULT_STYLE) -> DrawCommand:
    """A box around component ``iface:comp``, re-laid out by the agent every tick. The batchable
    form of :func:`highlight_component` (which can name its own key)."""
    geometry: dict[str, Any] = {"iface": _int_in(iface, 0, 0xFFFF, "iface"),
                                "comp": _int_in(comp, 0, 0xFFFF, "comp")}
    if label:
        geometry["label"] = _caption(label, "label")
    return _primitive(key, "component", False, None, geometry, style)


def _highlight(method: str, target: dict[str, Any], key: str | None, label: str | None,
               style: Style) -> Highlight:
    if key is not None:
        _key(key)
    if label is not None:
        _caption(label, "label")
    return Highlight(method, MappingProxyType(target), key, label, style)


def _footprint(w: int, h: int) -> dict[str, Any]:
    return {"w": _int_in(w, 1, MAX_WORLD_TILE, "w"), "h": _int_in(h, 1, MAX_WORLD_TILE, "h")}


def _entity(kind: str, index: int, key: str | None, w: int, h: int, label: str | None,
            style: Style) -> Highlight:
    target = {kind: _int_in(index, 0, 0x7FFFFFFF, "index"), **_footprint(w, h)}
    return _highlight("highlight_entity", target, key, label, style)


def highlight_npc(server_index: int, *, key: str | None = None, w: int = 1, h: int = 1,
                  label: str | None = None, style: Style = DEFAULT_STYLE) -> Highlight:
    """The NPC at ``server_index``, footprint ``w`` by ``h`` tiles, followed as it moves."""
    return _entity("npc", server_index, key, w, h, label, style)


def highlight_player(server_index: int, *, key: str | None = None, w: int = 1, h: int = 1,
                     label: str | None = None, style: Style = DEFAULT_STYLE) -> Highlight:
    """The player at ``server_index``, followed as it moves."""
    return _entity("player", server_index, key, w, h, label, style)


def highlight_self(*, key: str | None = None, w: int = 1, h: int = 1, label: str | None = None,
                   style: Style = DEFAULT_STYLE) -> Highlight:
    """The local player."""
    return _highlight("highlight_entity", {"self": True, **_footprint(w, h)}, key, label, style)


def _tile_xy(x: int, y: int, plane: int) -> dict[str, Any]:
    return {"x": _int_in(x, -MAX_WORLD_TILE, MAX_WORLD_TILE, "x"),
            "y": _int_in(y, -MAX_WORLD_TILE, MAX_WORLD_TILE, "y"),
            "plane": _int_in(plane, MIN_PLANE, MAX_PLANE, "plane")}


def highlight_tile(x: int, y: int, plane: int = 0, *, key: str | None = None,
                   label: str | None = None, style: Style = DEFAULT_STYLE) -> Highlight:
    """One world tile."""
    return _highlight("highlight_tile", _tile_xy(x, y, plane), key, label, style)


def highlight_area(x: int, y: int, w: int, h: int, plane: int = 0, *, key: str | None = None,
                   label: str | None = None, style: Style = DEFAULT_STYLE) -> Highlight:
    """A ``w`` by ``h`` block of tiles whose south-west corner is ``x, y``."""
    return _highlight("highlight_area", {**_tile_xy(x, y, plane), **_footprint(w, h)},
                      key, label, style)


def highlight_component(iface: int, comp: int, *, key: str | None = None,
                        label: str | None = None, style: Style = DEFAULT_STYLE) -> Highlight:
    """Component ``iface:comp``, re-laid out every tick."""
    target = {"iface": _int_in(iface, 0, 0xFFFF, "iface"),
              "comp": _int_in(comp, 0, 0xFFFF, "comp")}
    return _highlight("highlight_component", target, key, label, style)


@dataclass(frozen=True, slots=True)
class DrawBatchResult:
    """What ``debug_draw_set_batch`` applied. A batch that drew nothing still succeeds at the
    call level: read ``dropped``. ``error`` is the first refusal's text, or None."""
    applied: int
    dropped: int
    error: str | None


@dataclass(frozen=True, slots=True)
class DrawEntry:
    """One retained command, as ``debug_draw_list`` reports it.

    ``ttl_ms`` is what is LEFT, -1 for no expiry. ``project`` is the agent's projection state
    for a world command; ``resolved`` whether the renderer may draw it this frame, and ``rect``
    the resolved screen box. ``geom`` is the stored geometry as sent (world: sub-tiles)."""
    key: str
    kind: str
    space: str
    plane: int
    project: str
    color: int
    thickness: int
    z: int
    filled: bool
    closed: bool
    geom: tuple[int, int, int, int]
    resolved: bool
    rect: tuple[int, int, int, int]
    ttl_ms: int
    font: str
    text: str

    @classmethod
    def from_wire(cls, row: dict) -> "DrawEntry":
        return cls(str(row["key"]), str(row["kind"]), str(row["space"]), int(row["plane"]),
                   str(row["project"]), int(row["color"]), int(row["thickness"]), int(row["z"]),
                   bool(row["filled"]), bool(row["closed"]), _quad(row["geom"]),
                   bool(row["resolved"]), _quad(row["rect"]), int(row["ttl_ms"]),
                   str(row["font"]), str(row["text"]))


def _quad(values: Any) -> tuple[int, int, int, int]:
    a, b, c, d = (int(v) for v in values)
    return a, b, c, d


@dataclass(frozen=True, slots=True)
class DrawStats:
    """``debug_draw_stats``. The fields a script checks are typed; ``raw`` has all of them.

    ``has_presented`` False while ``enabled`` is True means the overlay has never delivered a
    frame. ``present_failures`` climbing means presenting is failing."""
    enabled: bool
    running: bool
    count: int
    capacity: int
    dropped: int
    has_presented: bool
    present_failures: int
    backend: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, reply: dict) -> "DrawStats":
        return cls(bool(reply["enabled"]), bool(reply["running"]), int(reply["count"]),
                   int(reply["capacity"]), int(reply["dropped"]), bool(reply["has_presented"]),
                   int(reply["present_failures"]), str(reply["backend"]),
                   MappingProxyType(dict(reply)))


def _field(method: str, reply: Any, name: str, kind: type) -> Any:
    value = reply.get(name) if isinstance(reply, dict) else None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise MalformedReply(method, f"{name} {value!r} in {reply!r}")
    return value


def _decoded(method: str, fn, value: Any) -> Any:
    try:
        return fn(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedReply(method, f"malformed reply: {exc!r}") from exc


class DebugDraw:
    """The overlay calls. Reach it as ``game.draw``; see the module docstring."""

    def __init__(self, rpc: "RpcClient") -> None:
        self._rpc = rpc

    def set(self, command: DrawCommand) -> str:
        """Set one primitive, returning its key. Raises ``RpcError`` when the agent refuses it
        (a full store, an exhausted text or poly slot, an out-of-range coordinate)."""
        if not isinstance(command, DrawCommand):
            raise TypeError(f"set takes a DrawCommand; a {type(command).__name__} goes to "
                            f"highlight()" if isinstance(command, Highlight) else
                            f"set takes a DrawCommand, not {type(command).__name__}")
        reply = self._rpc.call("debug_draw_set", command.wire())
        return _field("debug_draw_set", reply, "key", str)

    def set_batch(self, commands: Iterable[DrawCommand]) -> DrawBatchResult:
        """Set up to :data:`MAX_BATCH_ITEMS` primitives in one round trip.

        Per-item refusals do not raise: they count into ``dropped``. An ``RpcError`` from this
        call means the store is in an unknown state, because the agent applies items as it walks
        them; :meth:`list` tells you what is retained."""
        items = list(commands)
        if len(items) > MAX_BATCH_ITEMS:
            raise ValueError(f"a batch takes at most {MAX_BATCH_ITEMS} commands, got "
                             f"{len(items)}; the agent would refuse all of them")
        for i, command in enumerate(items):
            if not isinstance(command, DrawCommand):
                raise TypeError(f"item {i} is a {type(command).__name__}; a batch takes "
                                f"DrawCommand only (a Highlight is its own call)")
        reply = self._rpc.call("debug_draw_set_batch", {"items": [c.wire() for c in items]})
        error = reply.get("error") if isinstance(reply, dict) else None
        if error is not None and not isinstance(error, str):
            raise MalformedReply("debug_draw_set_batch", f"error {error!r}")
        return DrawBatchResult(_field("debug_draw_set_batch", reply, "count", int),
                               _field("debug_draw_set_batch", reply, "dropped", int), error)

    def highlight(self, highlight: Highlight) -> str:
        """Set one highlight, returning its key (the agent's auto key when it had none)."""
        if not isinstance(highlight, Highlight):
            raise TypeError(f"highlight takes a Highlight, not {type(highlight).__name__}")
        reply = self._rpc.call(highlight.method, highlight.wire())
        return _field(highlight.method, reply, "key", str)

    def clear(self, *keys: str) -> int:
        """Remove the named commands; returns how many went. Unknown keys are not an error."""
        if not keys:
            raise ValueError("clear needs at least one key (clear_all removes everything)")
        reply = self._rpc.call("debug_draw_clear", {"keys": [_key(k) for k in keys]})
        return _field("debug_draw_clear", reply, "removed", int)

    def clear_all(self) -> int:
        """Remove everything this connection drew; returns how many went. That includes the
        drawings of other scripts on the same ``Connection`` (see the module docstring).
        Never widened to the agent's ``scope: "all"``, which would erase other clients' too."""
        reply = self._rpc.call("debug_draw_clear_all", {"scope": "mine"})
        return _field("debug_draw_clear_all", reply, "removed", int)

    def list(self) -> tuple[DrawEntry, ...]:
        """Every command this connection holds: one round trip per 128."""
        out: list[DrawEntry] = []
        while True:
            reply = self._rpc.call("debug_draw_list", {"offset": len(out), "limit": LIST_PAGE})
            total = _field("debug_draw_list", reply, "total", int)
            rows = _field("debug_draw_list", reply, "items", list)
            out += _decoded("debug_draw_list", lambda r: [DrawEntry.from_wire(x) for x in r], rows)
            if not rows or len(out) >= total:
                return tuple(out)

    def enabled(self) -> bool:
        """Whether the agent is drawing at all."""
        reply = self._rpc.call("debug_draw_enable")
        return _field("debug_draw_enable", reply, "enabled", bool)

    def set_enabled(self, enabled: bool) -> bool:
        """Switch drawing on or off for the whole agent (every connection); returns the state
        that took effect."""
        reply = self._rpc.call("debug_draw_enable", {"enabled": bool(enabled)})
        return _field("debug_draw_enable", reply, "enabled", bool)

    def stats(self) -> DrawStats:
        """The overlay's counters and renderer health."""
        reply = self._rpc.call("debug_draw_stats")
        return _decoded("debug_draw_stats", DrawStats.from_wire, reply)
