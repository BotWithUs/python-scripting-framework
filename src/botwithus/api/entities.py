"""Fluent read-only queries over the snapshot's entity arrays.

These operate on a ``SnapshotView`` frame -- pure reads, no RPC. Example::

    nearest = Npcs(ctx.snapshot()).by_type(1234).nearest(me.tile)

Each query is a thin, chainable filter over the decoded entity list; terminal methods
(``all``, ``first``, ``nearest``, ``count``) materialise the result.
"""
from __future__ import annotations

import random as _random
from typing import TYPE_CHECKING, Any, Callable, Generic, Iterable, TypeVar

from .snapshot_types import GroundItem, HintArrow, LocationEntry, NpcEntry, PlayerEntry, Tile
from .world import position_of

if TYPE_CHECKING:
    from .interact import EntityDefs
    from .world import Area as WorldArea

_NO_ANIMATION = -1
_NO_ID = frozenset((-1,))
_PERCENT = 100

_T = TypeVar("_T")


def chebyshev(a: Tile, b: Tile) -> int:
    """Chebyshev (chessboard) tile distance, ignoring plane differences."""
    return max(abs(a.x - b.x), abs(a.y - b.y))


class EntityQuery(Generic[_T]):
    """``tile_of`` may return None for a row with no position (a hint arrow on an entity): that
    row is within no distance of anything, and ``nearest`` skips it."""

    def __init__(self, items: Iterable[_T], tile_of: Callable[[_T], Tile | None]) -> None:
        self._items = list(items)
        self._tile_of = tile_of

    def where(self, predicate: Callable[[_T], bool]) -> "EntityQuery[_T]":
        return EntityQuery((e for e in self._items if predicate(e)), self._tile_of)

    def within(self, origin: Tile, radius: int) -> "EntityQuery[_T]":
        def in_range(e: _T) -> bool:
            tile = self._tile_of(e)
            return tile is not None and chebyshev(tile, origin) <= radius
        return self.where(in_range)

    # --- J1 filters (JBotWithUsV2 EntityQuery / ActorFilters / AnimationFilters) --------
    #
    # Each reads a field the row may not have (a ground item has no animation): a row without
    # the field never matches. Names and options come from the cache (botwithus.api.interact).

    def _field_in(self, field: str, ids: Iterable[int]) -> "EntityQuery[_T]":
        wanted = frozenset(ids)
        return self.where(lambda e: getattr(e, field, None) in wanted)

    def with_ids(self, *ids: int) -> "EntityQuery[_T]":
        """Rows whose type id is any of ``ids``: an NPC by its base or active (morph) type, a
        scene object by its base, interact or resolved id, a ground item by its item id."""
        wanted = frozenset(ids)
        return self.where(lambda e: not wanted.isdisjoint(_type_ids(e)))

    def named_any_of(self, *names: str, defs: "EntityDefs | None" = None) -> "EntityQuery[_T]":
        """Rows whose whole name (cache, case-insensitive) is any of ``names``. Players match on
        their own name; NPCs, scene objects and ground items on their definition's."""
        wanted = frozenset(n.casefold() for n in names)
        resolve = _name_resolver(defs)
        return self.where(lambda e: (resolve(e) or "").casefold() in wanted)

    def with_option(self, option: str, defs: "EntityDefs | None" = None) -> "EntityQuery[_T]":
        """Rows whose right-click menu (cache) lists ``option``, case-insensitively."""
        from .interact import EntityDefs
        source = defs or EntityDefs()
        return self.where(lambda e: source.has_option(e, option))

    def inside(self, area: "WorldArea") -> "EntityQuery[_T]":
        return self.where(area.contains)

    def outside(self, area: "WorldArea") -> "EntityQuery[_T]":
        return self.where(lambda e: not area.contains(e))

    def on_plane(self, plane: int) -> "EntityQuery[_T]":
        return self.where(lambda e: getattr(e, "plane", None) == plane)

    def reachable(self, walker: Any) -> "EntityQuery[_T]":
        """Rows whose own tile the walker can reach (``walker.is_reachable``). Scenery that
        blocks its own tile (a booth, a tree) reads unreachable. One pathfind per row, so add
        it last."""
        return self.where(lambda e: (t := self._tile_of(e)) is not None and walker.is_reachable(t))

    def animating(self) -> "EntityQuery[_T]":
        return self.where(lambda e: getattr(e, "animation_id", _NO_ANIMATION) != _NO_ANIMATION)

    def not_animating(self) -> "EntityQuery[_T]":
        return self.where(lambda e: getattr(e, "animation_id", None) == _NO_ANIMATION)

    def with_animation(self, *ids: int) -> "EntityQuery[_T]":
        return self._field_in("animation_id", ids)

    def with_stance(self, *ids: int) -> "EntityQuery[_T]":
        return self._field_in("stance_id", ids)

    def with_spot_anim(self, *ids: int) -> "EntityQuery[_T]":
        return self._field_in("spot_anim_id", ids)

    def moving(self) -> "EntityQuery[_T]":
        return self.where(lambda e: getattr(e, "moving", False) is True)

    def not_moving(self) -> "EntityQuery[_T]":
        return self.where(lambda e: getattr(e, "moving", True) is False)

    def with_combat_level(self, low: int, high: int) -> "EntityQuery[_T]":
        return self.where(lambda e: low <= getattr(e, "combat_level", low - 1) <= high)

    def with_health(self, low: int, high: int) -> "EntityQuery[_T]":
        """NPCs with ``low <= hp <= high``. Players carry no HP on the snapshot."""
        return self.where(lambda e: isinstance(e, NpcEntry) and low <= e.hp <= high)

    def with_health_percent(self, low: int, high: int) -> "EntityQuery[_T]":
        """NPCs whose hp is ``low..high`` percent of max. An unknown max never matches."""
        return self.where(lambda e: isinstance(e, NpcEntry) and e.max_hp > 0
                          and low <= e.hp * _PERCENT // e.max_hp <= high)

    def all(self) -> list[_T]:
        return list(self._items)

    def last(self) -> _T | None:
        return self._items[-1] if self._items else None

    def random(self, rng: _random.Random | None = None) -> _T | None:
        return (rng or _random).choice(self._items) if self._items else None

    def nearest_to(self, x: Any, y: int | None = None, plane: int | None = None) -> _T | None:
        """The row nearest (Chebyshev) to ``(x, y)``, or to a tile / coordinate / entity given
        as the only argument (then on its plane). A plane, when known, must match."""
        if y is None:
            pos = position_of(x)
            if pos is None:
                raise TypeError(f"{x!r} has no tile position")
            x, y, plane = pos
        origin = Tile(int(x), int(y), -1 if plane is None else plane)
        placed = [(e, t) for e, t in ((e, self._tile_of(e)) for e in self._items)
                  if t is not None and (plane is None or t.plane == plane)]
        if not placed:
            return None
        return min(placed, key=lambda pair: chebyshev(pair[1], origin))[0]

    def count(self) -> int:
        return len(self._items)

    def first(self) -> _T | None:
        return self._items[0] if self._items else None

    def nearest(self, origin: Tile) -> _T | None:
        placed = [(e, t) for e, t in ((e, self._tile_of(e)) for e in self._items) if t is not None]
        if not placed:
            return None
        return min(placed, key=lambda pair: chebyshev(pair[1], origin))[0]


def Npcs(snapshot) -> EntityQuery[NpcEntry]:
    return EntityQuery(snapshot.npcs(), lambda e: e.tile)


def Players(snapshot) -> EntityQuery[PlayerEntry]:
    return EntityQuery(snapshot.players(), lambda e: e.tile)


def SceneObjects(snapshot) -> EntityQuery[LocationEntry]:
    return EntityQuery(snapshot.locations(), lambda e: e.tile)


def GroundItems(snapshot) -> EntityQuery[GroundItem]:
    return EntityQuery(snapshot.ground_items(), lambda e: e.tile)


def HintArrows(game) -> EntityQuery[HintArrow]:
    """Hint arrows over a ``GameApi``. Unlike the snapshot queries this makes one RPC, here.

    Only tile arrows have a position, so npc and player arrows never match ``within`` or
    ``nearest``, though ``all`` / ``first`` / ``where`` see them. A refusal reads as no arrows,
    and the rows do not carry the list's ``truncated``: call ``game.hint_arrows()`` to tell
    "none" from "refused" or "incomplete".
    """
    return EntityQuery(game.hint_arrows().arrows, lambda e: e.tile)


# Convenience filters commonly chained onto the queries above.

def by_type(query: EntityQuery, type_id: int) -> EntityQuery:
    return query.where(lambda e: getattr(e, "type_id", None) == type_id)


def by_item(query: EntityQuery[GroundItem], item_id: int) -> EntityQuery[GroundItem]:
    return query.where(lambda e: e.item_id == item_id)


_NO_BREAK_SPACE = "\u00a0"


def normalize_name(name: str) -> str:
    """A player name in the form names are compared in: U+00A0 (no-break space, which the
    client makes from cp1252 0xA0) becomes a plain space. Display the raw name; compare this."""
    return name.replace(_NO_BREAK_SPACE, " ")


def names_equal(a: str, b: str) -> bool:
    """Whether two player names are the same name. An empty name never equals anything,
    itself included: "" means the client has not decoded that player's name yet."""
    if not a or not b:
        return False
    return normalize_name(a) == normalize_name(b)


def by_name(query: EntityQuery[PlayerEntry], name: str) -> EntityQuery[PlayerEntry]:
    """Players whose name is ``name`` (see ``names_equal``). Players whose name is not
    decoded yet never match, and an empty ``name`` matches nobody."""
    return query.where(lambda e: names_equal(e.name, name))


def _type_ids(entity: Any) -> frozenset[int]:
    if isinstance(entity, NpcEntry):
        return frozenset((entity.type_id, entity.active_type_id)) - _NO_ID
    if isinstance(entity, LocationEntry):
        return frozenset((entity.type_id, entity.interact_id, entity.resolved_id)) - _NO_ID
    if isinstance(entity, GroundItem):
        return frozenset((entity.item_id,))
    type_id = getattr(entity, "type_id", None)
    return frozenset() if type_id is None else frozenset((type_id,))


def _name_resolver(defs: "EntityDefs | None") -> Callable[[Any], "str | None"]:
    from .interact import EntityDefs
    source = defs or EntityDefs()

    def name_of(entity: Any) -> str | None:
        if isinstance(entity, PlayerEntry):
            return normalize_name(entity.name) or None
        return source.name(entity)
    return name_of
