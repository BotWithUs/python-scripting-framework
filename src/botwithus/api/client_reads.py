"""Typed results of the client reads the agent added in NXTLibrary 0f746b4: Grand Exchange
offers, the world list, set_world, target mode, world_to_screen and interface children with
sub ids (PUBLIC_PROTOCOL.md 6.2.1, 6.2.2, 6.7).

Each ``*_from_reply`` raises :class:`MalformedReply` for a reply that is not the shape the
agent's handler writes (NXTLibrary ``src/rpc/GameReadHandlers.cpp``), so a key the agent never
sends fails loudly instead of reading as a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.rpc import MalformedReply

# --- Grand Exchange ----------------------------------------------------------------------

GE_SLOTS = 8
# The client's offer stages (``GeOffer.stage``). A slot is in use when stage != GE_STAGE_EMPTY.
GE_STAGE_EMPTY = 0
GE_STAGE_ADDING = 1
GE_STAGE_ADDED = 2
GE_STAGE_VERIFYING = 3
GE_STAGE_ABORTING = 4
GE_STAGE_FINISHED = 5
GE_TYPE_BUY = 0
GE_TYPE_SELL = 1


def _int(row: dict, key: str) -> int:
    v = row[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"{key} is not an int: {v!r}")
    return v


def _str(row: dict, key: str) -> str:
    v = row[key]
    if not isinstance(v, str):
        raise TypeError(f"{key} is not a string: {v!r}")
    return v


def _bool(row: dict, key: str) -> bool:
    v = row[key]
    if not isinstance(v, bool):
        raise TypeError(f"{key} is not a bool: {v!r}")
    return v


def _decode(method: str, fn, reply: Any):
    if not isinstance(reply, dict):
        raise MalformedReply(method, f"expected a map, got {reply!r}")
    try:
        return fn(reply)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedReply(method, f"malformed reply: {exc!r}") from exc


@dataclass(frozen=True, slots=True)
class GeOffer:
    """One Grand Exchange slot. ``price`` is per item; ``price`` and ``gold_from_sale`` can
    exceed 32 bits. An empty slot's other fields are whatever the client left there."""
    slot: int
    stage: int
    stage_name: str
    type: int
    type_name: str
    item_id: int
    item_count: int
    sold_count: int
    price: int
    gold_from_sale: int

    @property
    def in_use(self) -> bool:
        """The agent's predicate: ``stage != 0``."""
        return self.stage != GE_STAGE_EMPTY

    @property
    def is_sell(self) -> bool:
        return self.type == GE_TYPE_SELL


def ge_offers_from_reply(reply: Any) -> tuple[GeOffer, ...]:
    """``get_ge_offers``: ``{market, offers: [{slot, stage, stageName, type, typeName, itemId,
    itemCount, soldCount, price, goldFromSale}]}``."""
    def parse(r: dict) -> tuple[GeOffer, ...]:
        _int(r, "market")
        rows = r["offers"]
        if not isinstance(rows, list) or len(rows) > GE_SLOTS:
            raise ValueError(f"offers is not a list of at most {GE_SLOTS}: {rows!r}")
        return tuple(GeOffer(_int(o, "slot"), _int(o, "stage"), _str(o, "stageName"),
                             _int(o, "type"), _str(o, "typeName"), _int(o, "itemId"),
                             _int(o, "itemCount"), _int(o, "soldCount"), _int(o, "price"),
                             _int(o, "goldFromSale")) for o in rows)
    return _decode("get_ge_offers", parse, reply)


# --- World list ----------------------------------------------------------------------------

# The flag bits resolved live on 951-4. The other bits are unresolved: read ``flags`` raw.
WORLD_FLAG_MEMBERS = 0x1
WORLD_FLAG_SKILL_TOTAL = 0x80
WORLD_FLAG_CLASSIC_ONLY = 0x400000
WORLD_FLAG_EOC_ONLY = 0x800000


@dataclass(frozen=True, slots=True)
class World:
    """One row of the world-select list. ``ping`` is a hypothesis on the agent side and has read
    0 on every world."""
    id: int
    flags: int
    activity: str
    location_id: int
    location_name: str
    population: int
    ping: int

    @property
    def members(self) -> bool:
        return bool(self.flags & WORLD_FLAG_MEMBERS)

    @property
    def skill_total(self) -> bool:
        return bool(self.flags & WORLD_FLAG_SKILL_TOTAL)

    @property
    def classic_only(self) -> bool:
        return bool(self.flags & WORLD_FLAG_CLASSIC_ONLY)

    @property
    def eoc_only(self) -> bool:
        return bool(self.flags & WORLD_FLAG_EOC_ONLY)


@dataclass(frozen=True, slots=True)
class WorldList:
    """The world list as the client holds it. EMPTY is not an error: the game has not fetched
    it yet (every login through the lobby does). ``truncated`` is set past 512 worlds."""
    worlds: tuple[World, ...]
    total: int
    skipped: int
    truncated: bool

    @property
    def is_fetched(self) -> bool:
        return len(self.worlds) > 0

    def get(self, world_id: int) -> World | None:
        return next((w for w in self.worlds if w.id == world_id), None)


def world_list_from_reply(reply: Any) -> WorldList:
    """``get_world_list``: ``{count, total, skipped, truncated, worlds: [{id, flags, activity,
    locationId, locationName, population, ping}]}``."""
    def parse(r: dict) -> WorldList:
        rows = r["worlds"]
        if not isinstance(rows, list) or _int(r, "count") != len(rows):
            raise ValueError(f"count {r.get('count')!r} disagrees with the worlds")
        worlds = tuple(World(_int(w, "id"), _int(w, "flags"), _str(w, "activity"),
                             _int(w, "locationId"), _str(w, "locationName"),
                             _int(w, "population"), _int(w, "ping")) for w in rows)
        return WorldList(worlds, _int(r, "total"), _int(r, "skipped"), _bool(r, "truncated"))
    return _decode("get_world_list", parse, reply)


# --- set_world / hop -----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SetWorldResult:
    """``set_world``'s answer. ``started`` False is a refusal the script can act on; ``reason``
    is the agent's code (``unknown_world``, ``members_world``, ``hop_in_progress``, ...)."""
    started: bool
    reason: str = ""


def set_world_from_reply(reply: Any) -> SetWorldResult:
    def parse(r: dict) -> SetWorldResult:
        started = _bool(r, "started")
        return SetWorldResult(True) if started else SetWorldResult(False, _str(r, "reason"))
    return _decode("set_world", parse, reply)


@dataclass(frozen=True, slots=True)
class HopResult:
    """What :meth:`GameApi.hop_world` saw. ``arrived`` is the predicate: the client reported
    the world with its state back at the lobby (20) or the world (30). ``world_id`` and
    ``game_state`` are the last reads (37 is mid-transfer). ``stopped`` is True when a GUI stop
    ended the wait."""
    arrived: bool
    started: bool
    world_id: int
    game_state: int
    elapsed_s: float
    reason: str = ""
    stopped: bool = False


# --- target mode ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ComponentRef:
    """A component, or one dynamic child (``sub``) of it. ``sub`` -1 = none."""
    iface: int
    comp: int
    sub: int = -1

    @property
    def packed(self) -> int:
        return (self.iface << 16) | self.comp


@dataclass(frozen=True, slots=True)
class TargetMode:
    """``get_target_mode``. ``active`` is the predicate: an item selected with Use, or a spell
    or ability armed. Inactive reads ``obj_id`` -1, ``source`` None and empty text."""
    active: bool
    is_item: bool
    obj_id: int
    source: ComponentRef | None
    verb: str
    name: str
    target_mask: int


def target_mode_from_reply(reply: Any) -> TargetMode:
    def parse(r: dict) -> TargetMode:
        src = r["source"]
        source = None if src is None else ComponentRef(_int(src, "iface"), _int(src, "comp"),
                                                       _int(src, "sub"))
        return TargetMode(_bool(r, "active"), _bool(r, "isItem"), _int(r, "objId"), source,
                          _str(r, "verb"), _str(r, "name"), _int(r, "targetMask"))
    return _decode("get_target_mode", parse, reply)


# --- world_to_screen -----------------------------------------------------------------------

WORLD_TO_SCREEN_MAX = 64
PROJECT_OK = "ok"
PROJECT_OFF_VIEWPORT = "off_viewport"
PROJECT_BEHIND_CAMERA = "behind_camera"
PROJECT_UNAVAILABLE = "unavailable"
PROJECT_WRONG_PLANE = "wrong_plane"


@dataclass(frozen=True, slots=True)
class ScreenPoint:
    """One projected tile centre. ``on_screen`` is the predicate (only for ``result`` "ok").
    ``x``/``y`` are window client pixels, None for behind_camera, unavailable and wrong_plane;
    off_viewport carries real ones outside the viewport."""
    result: str
    on_screen: bool
    x: int | None
    y: int | None


@dataclass(frozen=True, slots=True)
class Viewport:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Projection:
    viewport: Viewport | None
    points: tuple[ScreenPoint, ...]


def _opt_int(row: dict, key: str) -> int | None:
    return None if row[key] is None else _int(row, key)


def projection_from_reply(reply: Any, count: int) -> Projection:
    """``world_to_screen``: ``{viewport: {x, y, width, height} | nil, points: [{result,
    onScreen, x | nil, y | nil}]}``, exactly one point per tile sent."""
    def parse(r: dict) -> Projection:
        rows = r["points"]
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError(f"expected {count} points, got {rows!r}")
        vp = r["viewport"]
        viewport = None if vp is None else Viewport(_int(vp, "x"), _int(vp, "y"),
                                                    _int(vp, "width"), _int(vp, "height"))
        points = tuple(ScreenPoint(_str(p, "result"), _bool(p, "onScreen"), _opt_int(p, "x"),
                                   _opt_int(p, "y")) for p in rows)
        return Projection(viewport, points)
    return _decode("world_to_screen", parse, reply)


# --- children with sub ids -----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Child:
    """One child as ``{comp, sub}``. A DYNAMIC child's ``comp`` is its parent's id: ``sub``
    tells it apart and is the ``param2`` of a COMPONENT (57) or SELECT_COMPONENT_ITEM (58)
    action on it. -1 = unset."""
    comp: int
    sub: int


def children_from_reply(method: str, reply: Any) -> tuple[Child, ...]:
    """``get_*_children {withSub: true}``: ``{children: [{comp, sub}]}``. A flat ``[int]`` list
    (an agent before withSub) is malformed here, not read as subs."""
    def parse(r: dict) -> tuple[Child, ...]:
        rows = r["children"]
        if not isinstance(rows, list):
            raise ValueError(f"children is not a list: {rows!r}")
        return tuple(Child(_int(c, "comp"), _int(c, "sub")) for c in rows)
    return _decode(method, parse, reply)


# --- execute_script ------------------------------------------------------------------------

SCRIPT_ARGS_MAX = 16
SCRIPT_STRING_ARG_MAX = 255   # UTF-8 bytes per string argument
SCRIPT_RETURN_KINDS = ("int", "long", "string")
