"""Typed records for the native host's walk progress surface (``bwu_walk_*``).

The host records every WorldWalker event during a walk, the step and transition a failed walk
failed on, and the transition it last attempted as *observed* through the host's callbacks
(the loc it tried to interact with, the NPC it tried to click, the chain steps it ran). These
dataclasses decode the dicts ``botwithus._native`` returns; field meanings follow
``bwu_host_surface.h`` (``BwuWalkResult`` / ``BwuWalkEvent`` / ``BwuWalkTransition`` / ``BwuTransitionInfo``).

Two views of a transition, kept apart on purpose: a :class:`WalkTransition` is what the walker
was *seen to do* (the post-resolution interact and chain calls), and its ``baked`` field, a
:class:`TransitionInfo`, is what the walker's *data says* the transition is (the record
verbatim: a ClickItem carries both the worn and the backpack variant, Wait steps included).
``baked`` is None for index -1, for no such transition, and on a host or worldwalker.dll that
cannot describe transitions.

The constants mirror the host's ``WALK_*`` / ``WALK_EV_*`` / ``WALK_TX_*``; they are restated
here so a script can name them without the native module (tests, type checking).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..api.snapshot_types import Tile

# BwuWalkResult.status
WALK_RUNNING = 0
WALK_ARRIVED = 1
WALK_FAILED = 2
WALK_CANCELLED = 3
WALK_TIMED_OUT = 4
WALK_ERROR = 5

# BwuWalkEvent.kind (WorldWalker WW_EVENT_*)
WALK_EV_STEP_ADVANCED = 0
WALK_EV_WALKING_TO_INTERACT = 1
WALK_EV_TELEPORT_INITIATED = 2
WALK_EV_STUCK = 3
WALK_EV_REPLAN_STARTED = 4
WALK_EV_ARRIVED = 5
WALK_EV_FAILED = 6
WALK_EV_NONE = -1

# BwuWalkTransition.kind (what the host saw the walker do)
WALK_TX_NONE = 0
WALK_TX_OBJECT = 1
WALK_TX_NPC = 2
WALK_TX_CHAIN = 3
WALK_TX_TELEPORT = 4

# Capacities of the host's fixed arrays; a total above one means the list was cut.
# Walk plan options (bwu_host_surface.h BWU_MOVE_* / BWU_RESTRICT_FREE_TO_PLAY). MOVE_* are
# BIT NUMBERS: switch a category off with disabled_moves=(1 << MOVE_DOORS) | ...; see
# moves_mask. RESTRICT_FREE_TO_PLAY is already a mask bit.
MOVE_DOORS = 0
MOVE_SHORTCUTS = 1
MOVE_PLANE = 2
MOVE_CLIMBOVERS = 3
MOVE_TRANSPORTS = 4
MOVE_TELEPORTS = 5
MOVE_LODESTONES = 6
MOVE_FAIRY_RINGS = 7
MOVE_SPIRIT_TREES = 8
MOVE_GLIDERS = 9
MOVE_CHARTERS = 10
MOVE_MAGIC_CARPETS = 11
MOVE_OTHER_CHAINS = 12
RESTRICT_FREE_TO_PLAY = 1 << 31
WALK_MAX_EXCLUDED = 256         # most transition indices one walk / path may exclude


def moves_mask(*categories: int) -> int:
    """The disabled_moves mask switching off each MOVE_* category given."""
    mask = 0
    for c in categories:
        if not MOVE_DOORS <= c <= MOVE_OTHER_CHAINS:
            raise ValueError(f"not a MOVE_* category: {c!r}")
        mask |= 1 << c
    return mask


WALK_MAX_CHAIN = 8              # BwuWalkTransition.chain
WALK_MAX_TRANSITIONS = 128      # the walk's transition log
TX_MAX_REQUIREMENTS = 16        # BwuTransitionInfo.requirements
TX_MAX_CHAIN_STEPS = 16         # BwuTransitionInfo.chain

# BwuTransitionInfo.origin -- branch on this
TX_ORIGIN_LOC = 0       # interact(object_id, origin_tile, option_index), then the chain
TX_ORIGIN_NPC = 1       # chain[0] is ClickNpc; origin_tile is the search centre
TX_ORIGIN_GLOBAL = 2    # a teleport usable from (almost) anywhere; origin_tile zero

# BwuTransitionInfo.kind
TX_KIND_TRANSPORT = 0
TX_KIND_FAIRY_RING = 1
TX_KIND_TELEPORT_CHAIN = 2
TX_KIND_SPELL = 3
TX_KIND_LODESTONE = 4
TX_KIND_ITEM_TELEPORT = 5

# BwuRequirement.kind
REQ_SKILL = 0               # id = skill, amount = level
REQ_ITEM = 1                # id = item, amount = count (worn + carried)
REQ_VARBIT = 2              # amount = value, matched exactly
REQ_VARP = 3                # amount = value, matched exactly
REQ_VARBIT_AT_LEAST = 4     # amount = minimum value
REQ_VARP_AT_LEAST = 5       # amount = minimum value
REQ_VARP_BIT = 6            # amount = bit index (0..31) that must be set


@dataclass(frozen=True, slots=True)
class WalkEvent:
    kind: int               # WALK_EV_*
    step_index: int         # index into the walker's current plan, -1 n/a
    transition_index: int   # the walker's transition record index, -1 n/a
    interaction_hint: int   # 1 + path tiles to the next interaction; 0 unknown
    t_ms: int               # ms since the walk started
    replans: int            # re-plans so far

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "WalkEvent":
        return cls(int(d["kind"]), int(d["step_index"]), int(d["transition_index"]),
                   int(d["interaction_hint"]), int(d["t_ms"]), int(d["replans"]))


@dataclass(frozen=True, slots=True)
class WalkChainStep:
    kind: int               # WorldWalker ChainStepKind
    args: tuple[int, ...]   # the nine generic slots a..i, verbatim

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "WalkChainStep":
        return cls(int(d["kind"]), tuple(int(a) for a in d["args"]))


def _tile(d: dict[str, Any]) -> Tile:
    return Tile(int(d["x"]), int(d["y"]), int(d["plane"]))


@dataclass(frozen=True, slots=True)
class Requirement:
    kind: int     # REQ_*
    id: int
    amount: int

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "Requirement":
        return cls(int(d["kind"]), int(d["id"]), int(d["amount"]))


@dataclass(frozen=True, slots=True)
class TransitionInfo:
    """The walker's baked record for a transition (``BwuTransitionInfo``). Fields that do not
    apply to ``origin`` read -1 (ids, option, radius) or zero (tiles, shape, rotation, code)."""

    origin: int                 # TX_ORIGIN_*
    kind: int                   # TX_KIND_*
    move_category: int          # WorldWalker WW_MOVE_* bit number
    is_runtime_loaded: bool     # True: a teleport from the JSONs; False: baked
    origin_tile: Tile
    dest_tile: Tile
    object_id: int              # LOC: loc id; else -1
    npc_type_min: int           # NPC: inclusive type id range; else -1
    npc_type_max: int
    npc_search_radius: int      # NPC: Chebyshev radius around origin_tile
    option_index: int           # 0-based; GLOBAL: -1
    shape: int                  # LOC: as baked; else 0
    rotation: int
    code: str                   # fairy-ring code; "" when none
    cost: float                 # the planner's tick cost
    requirement_count: int      # true total; ``requirements`` keeps the first TX_MAX_REQUIREMENTS
    chain_step_count: int       # true total; ``chain`` keeps the first TX_MAX_CHAIN_STEPS
    requirements: tuple[Requirement, ...]
    chain: tuple[WalkChainStep, ...]

    @property
    def requirements_cut(self) -> bool:
        return self.requirement_count > len(self.requirements)

    @property
    def chain_cut(self) -> bool:
        return self.chain_step_count > len(self.chain)

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "TransitionInfo":
        return cls(int(d["origin"]), int(d["kind"]), int(d["move_category"]),
                   bool(d["is_runtime_loaded"]), _tile(d["origin_tile"]), _tile(d["dest_tile"]),
                   int(d["object_id"]), int(d["npc_type_min"]), int(d["npc_type_max"]),
                   int(d["npc_search_radius"]), int(d["option_index"]), int(d["shape"]),
                   int(d["rotation"]), str(d["code"]), float(d["cost"]),
                   int(d["requirement_count"]), int(d["chain_step_count"]),
                   tuple(Requirement.from_native(r) for r in d["requirements"]),
                   tuple(WalkChainStep.from_native(c) for c in d["chain"]))

    @classmethod
    def from_native_or_none(cls, d: dict[str, Any] | None) -> "TransitionInfo | None":
        return None if d is None else cls.from_native(d)


@dataclass(frozen=True, slots=True)
class WalkTransition:
    """The transition the walker last started, as observed. ``kind`` is WALK_TX_NONE when
    none started, or when it failed before issuing any callback (only the indexes are set)."""

    kind: int
    transition_index: int
    step_index: int
    object_id: int          # OBJECT: loc id; NPC: type range low; else -1
    npc_type_hi: int        # NPC: type range high; else -1
    tile: Tile              # OBJECT: interact origin; NPC: search centre
    option_index: int       # 0-based; -1 n/a
    interact_attempts: int
    interact_issued: int    # 0 with attempts > 0: the loc was not found
    chain_count: int        # total seen; ``chain`` keeps the first WALK_MAX_CHAIN
    chain: tuple[WalkChainStep, ...]
    # The walker's record for transition_index; None for -1, no such transition, or a host /
    # worldwalker.dll that cannot describe (a host without walk_describe never sends it).
    baked: TransitionInfo | None = None

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "WalkTransition":
        chain = tuple(WalkChainStep.from_native(c) for c in d["chain"])
        return cls(int(d["kind"]), int(d["transition_index"]), int(d["step_index"]),
                   int(d["object_id"]), int(d["npc_type_hi"]), _tile(d["tile"]),
                   int(d["option_index"]), int(d["interact_attempts"]),
                   int(d["interact_issued"]), int(d["chain_count"]), chain,
                   TransitionInfo.from_native_or_none(d.get("baked")))


@dataclass(frozen=True, slots=True)
class WalkResult:
    status: int             # WALK_*
    result: int             # the BwuResult walk() is built on (0 on arrival)
    final_event: int        # WALK_EV_* of the last event, WALK_EV_NONE if none
    fail_step: int          # from the walker's FAILED event, else -1
    fail_transition: int
    replans: int
    event_count: int
    events_dropped: int
    elapsed_ms: int
    transition: WalkTransition
    error: str
    events: tuple[WalkEvent, ...] = ()   # filled by walk_ex; empty from status/wait
    # Every transition the walk attempted, in order (a retry after a re-plan is a new entry),
    # each with ``baked``. Filled by walk_ex on a host with the transition log; empty from
    # status/wait and from an older host -- NativeHost.walk_transitions() reads it any time.
    transitions: tuple[WalkTransition, ...] = ()
    # How many the walk attempted, past WALK_MAX_TRANSITIONS included; 0 when not reported.
    transitions_attempted: int = 0

    @property
    def is_running(self) -> bool:
        return self.status == WALK_RUNNING

    @property
    def arrived(self) -> bool:
        return self.status == WALK_ARRIVED

    @property
    def failed_on_transition(self) -> WalkTransition | None:
        """The observed transition the walk failed on, or None if it failed elsewhere."""
        if self.fail_transition < 0 or self.transition.transition_index != self.fail_transition:
            return None
        return self.transition

    @property
    def transitions_cut(self) -> bool:
        """True when the walk attempted more transitions than the host's log kept."""
        return self.transitions_attempted > len(self.transitions)

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "WalkResult":
        events = tuple(WalkEvent.from_native(e) for e in d.get("events", ()))
        txs = tuple(WalkTransition.from_native(t) for t in d.get("transitions", ()))
        return cls(int(d["status"]), int(d["result"]), int(d["final_event"]),
                   int(d["fail_step"]), int(d["fail_transition"]), int(d["replans"]),
                   int(d["event_count"]), int(d["events_dropped"]), int(d["elapsed_ms"]),
                   WalkTransition.from_native(d["transition"]), str(d["error"]), events,
                   txs, int(d.get("transitions_attempted", 0)))


# --- avoid areas and route links (native ABI 7, WorldWalker #44) -----------------------------

AREA_ALL_PLANES = -1     # an Area on every plane (BWU_AREA_ALL_PLANES)
WALK_MAX_AVOID = 64      # most avoid areas one walk / path may carry (BWU_WALK_MAX_AVOID)
_MAX_PLANE = 3


@dataclass(frozen=True, slots=True)
class Area:
    """An inclusive tile rectangle the walker never routes through (``BwuArea``).

    On one plane, or on every plane (:data:`AREA_ALL_PLANES`, the default). What the walker does
    with it: no walk step lands on its tiles, no door, boat or teleport is taken that starts or
    lands in it, and the walk clicks round it a step at a time when near. An area holding the
    start or the goal tile is lifted for that plan. A route that can only cross one fails with
    no route; it is never quietly taken through it. A polygon is passed as the rectangles that
    cover it."""

    min_x: int
    min_y: int
    max_x: int
    max_y: int
    plane: int = AREA_ALL_PLANES

    def __post_init__(self) -> None:
        if self.plane != AREA_ALL_PLANES and not 0 <= self.plane <= _MAX_PLANE:
            raise ValueError(f"an area's plane is 0..3 or AREA_ALL_PLANES, got {self.plane!r}")
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError(f"an area needs min <= max: {self!r}")

    @classmethod
    def box(cls, x1: int, y1: int, x2: int, y2: int, plane: int = AREA_ALL_PLANES) -> "Area":
        """The rectangle with corners (x1, y1) and (x2, y2), in either order."""
        return cls(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), plane)

    @classmethod
    def around(cls, x: int, y: int, radius: int, plane: int = AREA_ALL_PLANES) -> "Area":
        """Every tile within ``radius`` (Chebyshev) of (x, y)."""
        if radius < 0:
            raise ValueError("radius must be >= 0")
        return cls(x - radius, y - radius, x + radius, y + radius, plane)

    def contains(self, x: int, y: int, plane: int) -> bool:
        on_plane = self.plane == AREA_ALL_PLANES or self.plane == plane
        return on_plane and self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def to_native(self) -> tuple[int, int, int, int, int]:
        """The ``(plane, min_x, min_y, max_x, max_y)`` tuple ``_native`` takes."""
        return (self.plane, self.min_x, self.min_y, self.max_x, self.max_y)


@dataclass(frozen=True, slots=True)
class RouteLink:
    """One transport or teleport a planned route takes (``BwuRouteLink``), in route order.

    ``from_tile`` is where the player stands to take it (the approach tile for a door or an NPC;
    for a global teleport, the tile it is cast from), ``to_tile`` where it lands. ``name`` is a
    runtime-loaded teleport's dataset name ("Lumbridge lodestone"); every baked transport has
    ``""`` -- name a loc by ``object_id`` through the cache (``Definitions.loc``)."""

    step_index: int          # index of the transition step in Route.steps
    transition_index: int    # as WalkEvent / walk_describe take it
    kind: int                # TX_KIND_*
    move_category: int       # MOVE_* bit number
    origin: int              # TX_ORIGIN_*
    object_id: int           # loc id (LOC), lowest NPC type id (NPC), -1 (GLOBAL)
    from_tile: Tile
    to_tile: Tile
    code: str                # fairy-ring code, "" otherwise
    name: str

    @property
    def is_teleport(self) -> bool:
        """A teleport usable from (almost) anywhere: a lodestone, spell or item."""
        return self.origin == TX_ORIGIN_GLOBAL

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "RouteLink":
        fx, fy, fp = d["from"]
        tx, ty, tp = d["to"]
        return cls(int(d["step_index"]), int(d["transition_index"]), int(d["kind"]),
                   int(d["move_category"]), int(d["origin"]), int(d["object_id"]),
                   Tile(int(fx), int(fy), int(fp)), Tile(int(tx), int(ty), int(tp)),
                   str(d["code"]), str(d["name"]))
