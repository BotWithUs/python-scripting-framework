"""Entity names and right-click options from the cache, and clicking an option by name.

The snapshot carries ids, not names or menus. NPC, scene-object and ground-item names and
options come from the cache definitions (:mod:`botwithus.api.definitions`, native ABI 7), keyed
the way the game keys them: an NPC by its **active** (morph-resolved) type, a scene object by its
**resolved** id, a ground item by its item id. The click itself is addressed by the base ids
(:mod:`botwithus.api.actions`).

The Java host's ``Npc.interact(String)`` / ``SceneObject.interact(String)`` /
``GroundItem.interact(String)`` and their ``hasOption``. Players have no option list anywhere on
the wire (the Java host has the same gap), so they are not handled here.

Every lookup here goes through a :class:`EntityDefs` (the cache by default). A lookup the cache
cannot answer (no cache, an old bwu_host) reads as no name and no options, never as an error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.native import NativeUnavailable
from .actions import (ENTITY_OPTION_SLOTS, GameAction, ground_item_action, npc_action,
                      object_action)
from .definitions import CacheUnavailable, definitions
from .snapshot_types import GroundItem, LocationEntry, NpcEntry

if TYPE_CHECKING:
    from .game import GameApi

_NO_TYPE = -1


class EntityDefs:
    """Names and options of NPCs, scene objects and ground items, read from the cache once per
    id. Pass your own (any object with ``npc``/``loc``/``item`` returning a def or None) to tests
    or to read something else."""

    def __init__(self, source: Any = None) -> None:
        self._source = source if source is not None else definitions
        self._seen: dict[tuple[str, int], Any] = {}

    def _get(self, kind: str, def_id: int) -> Any:
        key = (kind, def_id)
        if key not in self._seen:
            try:
                self._seen[key] = getattr(self._source, kind)(def_id)
            except (CacheUnavailable, NativeUnavailable):
                return None  # not memoised: the cache may come up later
        return self._seen[key]

    def definition(self, entity: Any) -> Any:
        if isinstance(entity, NpcEntry):
            # The morph-resolved type names the NPC; -1 is a morph that resolves to nothing (the
            # NPC is hidden), which has no name or options.
            active = entity.active_type_id
            return None if active == _NO_TYPE else self._get("npc", active)
        if isinstance(entity, LocationEntry):
            resolved = entity.resolved_id
            return self._get("loc", resolved if resolved != _NO_TYPE else entity.type_id)
        if isinstance(entity, GroundItem):
            return self._get("item", entity.item_id)
        return None

    def name(self, entity: Any) -> str | None:
        """The entity's name, or None when unknown or nameless."""
        found = self.definition(entity)
        return (found.name or None) if found is not None else None

    def options(self, entity: Any) -> tuple[str, ...]:
        """The right-click options in slot order ("" for an empty slot); () when unknown."""
        found = self.definition(entity)
        if found is None:
            return ()
        if isinstance(entity, GroundItem):
            return tuple(found.ground_ops)
        return tuple(found.ops)

    def option_slot(self, entity: Any, option: str) -> int | None:
        """The 1-based slot of ``option`` (case-insensitive, exact), or None."""
        wanted = option.casefold()
        for index, name in enumerate(self.options(entity)):
            if name and name.casefold() == wanted and index < ENTITY_OPTION_SLOTS:
                return index + 1
        return None

    def has_option(self, entity: Any, option: str) -> bool:
        return self.option_slot(entity, option) is not None


def option_action(entity: Any, slot: int) -> GameAction:
    """The click for option ``slot`` on an NPC, scene object or ground item row."""
    if isinstance(entity, NpcEntry):
        return npc_action(entity, slot)
    if isinstance(entity, LocationEntry):
        return object_action(entity, slot)
    if isinstance(entity, GroundItem):
        return ground_item_action(entity, slot)
    raise TypeError(f"no option clicks for {type(entity).__name__}")


def interact(game: "GameApi", entity: Any, option: str, defs: EntityDefs | None = None) -> bool:
    """Queue the click for ``option`` on ``entity``. False, with nothing sent, when the entity is
    None or lists no such option."""
    if entity is None:
        return False
    slot = (defs or EntityDefs()).option_slot(entity, option)
    if slot is None:
        return False
    game.queue_action(option_action(entity, slot))
    return True
