"""Action ids and the flat action shape the agent's action queue dispatches.

``action_id`` selects one of the game's menu-action entry points; ``param1..3`` are
that entry point's own arguments. The id numbering is the AGENT's own stable mapping
(PUBLIC_PROTOCOL.md section 6.3.1) -- it is not a contiguous enum you can read out of
the client, so this table is the source of truth. An id omitted here cannot be looked
up elsewhere.

Option ids are 1-based right-click menu slots: slot 1 is the default left-click action.
Objects/NPCs/ground items have six slots each; players have ten. Read an entity's option
strings from the snapshot to map a label onto a slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import-time cycle: snapshot_types must not depend on actions
    from .snapshot_types import GroundItem, LocationEntry, NpcEntry


@dataclass(frozen=True, slots=True)
class GameAction:
    """One queued action: an opcode plus three integer parameters."""
    action_id: int
    param1: int = 0
    param2: int = 0
    param3: int = 0


# Menu slot counts. Objects, NPCs and ground items expose six right-click
# options each; players expose ten.
ENTITY_OPTION_SLOTS = 6
PLAYER_OPTION_SLOTS = 10


def _option_id(table: tuple[int, ...], slot: int, entity: str) -> int:
    """``table[slot - 1]`` with the range checked first.

    The check is the point. Python's negative indexing means ``slot=0`` reads
    ``table[-1]`` and returns the LAST option id -- so asking for a slot that
    does not exist used to dispatch the sixth menu entry instead of failing.
    ``slot=-1`` returned the fifth, and so on up the table. A wrong action is
    silently sent to the game and the caller has no way to notice.
    """
    if not 1 <= slot <= len(table):
        raise ValueError(f"{entity} option slot must be 1..{len(table)}, got {slot}")
    return table[slot - 1]


class ActionType:
    """Agent action-id constants (see module docstring)."""

    WALK = 23  # param1=1, param2=tileX, param3=tileY

    OBJECT1 = 3
    OBJECT2 = 4
    OBJECT3 = 5
    OBJECT4 = 6
    OBJECT5 = 1001
    OBJECT6 = 1002  # param1=loc typeId, param2=tileX, param3=tileY

    NPC1 = 9
    NPC2 = 10
    NPC3 = 11
    NPC4 = 12
    NPC5 = 13
    NPC6 = 1003  # param1=npc serverIndex, param2=0, param3=0

    GROUND_ITEM1 = 18
    GROUND_ITEM2 = 19
    GROUND_ITEM3 = 20
    GROUND_ITEM4 = 21
    GROUND_ITEM5 = 22
    GROUND_ITEM6 = 1004  # param1=item id, param2=tileX, param3=tileY

    # PLAYER1..PLAYER10 = 44..53; param1=player serverIndex, param2=0, param3=0
    PLAYER1 = 44

    COMPONENT = 57   # param1=1-based option, param2=sub-slot(-1 none), param3=(iface<<16)|comp
    DIALOGUE = 30    # param1=0, param2=-1, param3=(iface<<16)|comp
    # Fire a component's CS2 trigger: param1=(iface<<16)|comp,
    # param2=(triggerType<<16)|(sub & 0xFFFF), param3=the trigger's arg (see api.input for keys)
    COMPONENT_TRIGGER = 5003

    # "Use X on Y" (target mode), PUBLIC_PROTOCOL.md 6.3.1. Arm with SELECT_COMPONENT, never
    # with COMPONENT: only 25 arms target mode. Then one target action; the agent ends target
    # mode after it. Ground item (17), player (15 / 16) and tile (59) packing is not documented.
    SELECT_COMPONENT = 25       # param1=0, param2=source sub (-1 none), param3=(iface<<16)|comp
    SELECT_COMPONENT_ITEM = 58  # param1=0, param2=target sub, param3=(iface<<16)|comp
    SELECT_NPC = 8              # param1=npc serverIndex, param2=0, param3=0
    SELECT_OBJECT = 2           # param1=loc typeId, param2=tileX, param3=tileY

    @staticmethod
    def object_option(slot: int) -> int:
        """The OBJECT<slot> action id for a 1-based menu slot (1..6).

        :raises ValueError: for a slot outside 1..6.
        """
        return _option_id((ActionType.OBJECT1, ActionType.OBJECT2, ActionType.OBJECT3,
                           ActionType.OBJECT4, ActionType.OBJECT5, ActionType.OBJECT6),
                          slot, "object")

    @staticmethod
    def npc_option(slot: int) -> int:
        """The NPC<slot> action id for a 1-based menu slot (1..6).

        :raises ValueError: for a slot outside 1..6.
        """
        return _option_id((ActionType.NPC1, ActionType.NPC2, ActionType.NPC3,
                           ActionType.NPC4, ActionType.NPC5, ActionType.NPC6),
                          slot, "npc")

    @staticmethod
    def ground_item_option(slot: int) -> int:
        """The GROUND_ITEM<slot> action id for a 1-based menu slot (1..6).

        :raises ValueError: for a slot outside 1..6.
        """
        return _option_id((ActionType.GROUND_ITEM1, ActionType.GROUND_ITEM2,
                           ActionType.GROUND_ITEM3, ActionType.GROUND_ITEM4,
                           ActionType.GROUND_ITEM5, ActionType.GROUND_ITEM6),
                          slot, "ground item")

    @staticmethod
    def player_option(slot: int) -> int:
        """PLAYER<slot> action id for a 1-based slot (1..10).

        Arithmetic rather than a table, so an out-of-range slot produced a
        plausible-looking id that is not a player option at all: slot 0 gave 43
        and slot 11 gave 54, neither of which is in PLAYER1..PLAYER10.

        :raises ValueError: for a slot outside 1..10.
        """
        if not 1 <= slot <= PLAYER_OPTION_SLOTS:
            raise ValueError(f"player option slot must be 1..{PLAYER_OPTION_SLOTS}, got {slot}")
        return ActionType.PLAYER1 + (slot - 1)


def walk_to(tile_x: int, tile_y: int) -> GameAction:
    """Queue a walk to an absolute world tile."""
    return GameAction(ActionType.WALK, 1, tile_x, tile_y)


def component_click(iface_id: int, comp_id: int, option: int = 1, sub_slot: int = -1) -> GameAction:
    """Click a component option (or an inventory-grid slot via ``sub_slot``)."""
    return GameAction(ActionType.COMPONENT, option, sub_slot, (iface_id << 16) | comp_id)


def dialogue_option(iface_id: int, comp_id: int) -> GameAction:
    """Select a multi-choice dialogue option (not a plain COMPONENT click)."""
    return GameAction(ActionType.DIALOGUE, 0, -1, (iface_id << 16) | comp_id)


# The backpack's slot grid. Mirrors JBotWithUsV2
# api/.../inventory/Backpack.java:17-18 (INTERFACE_ID / COMPONENT_ID), which
# also records the backing inventory as inv id 93.
BACKPACK_IFACE = 1473
BACKPACK_COMPONENT = 5


def loc_base_id(loc: LocationEntry) -> int:
    """The loc id an interaction is addressed to.

    A combined section carries it in ``type_id``; a direct LOCATION carries it in
    ``interact_id`` (and a section's ``interact_id`` is always -1). This is the
    base id, deliberately NOT ``resolved_id`` -- the morph-resolved id is for
    name and option lookups only. Mirrors JBotWithUsV2 ``Location.baseId()``,
    which its scene-object facade feeds straight into the action's param1
    (``SceneObjects.java:152-161``, commented "handle (action param1)").
    """
    return loc.type_id if loc.is_combined_section else loc.interact_id


def object_action(loc: LocationEntry, slot: int) -> GameAction:
    """Right-click option ``slot`` (1..6) on a scene location.

    Shape per the OBJECT ids above: ``param1`` = loc id, ``param2`` = tileX,
    ``param3`` = tileY.
    """
    return GameAction(ActionType.object_option(slot), loc_base_id(loc), loc.tile_x, loc.tile_y)


def npc_action(npc: NpcEntry, slot: int) -> GameAction:
    """Right-click option ``slot`` (1..6) on an NPC.

    Shape per the NPC ids above: ``param1`` = the NPC's server index, the other
    two unused. An NPC is addressed by index, not by tile -- it moves.
    """
    return GameAction(ActionType.npc_option(slot), npc.server_index, 0, 0)


def ground_item_action(item: GroundItem, slot: int) -> GameAction:
    """Right-click option ``slot`` (1..6) on a ground item.

    Shape per the GROUND_ITEM ids above: ``param1`` = item id, ``param2`` =
    tileX, ``param3`` = tileY.
    """
    return GameAction(ActionType.ground_item_option(slot), item.item_id, item.tile_x, item.tile_y)


def backpack_action(slot: int, option: int) -> GameAction:
    """Click backpack ``slot`` with 1-based right-click ``option``.

    ``slot`` is the 0-based grid position inside the backpack, which rides in the
    COMPONENT action's ``sub_slot``; ``option`` is the 1-based menu index.

    ``option`` has NO default on purpose. The drop index is a property of the
    ITEM, not of the backpack: it is the item's position in that item's own
    right-click menu, so it moves when the item has more or fewer inventory
    options. The Java host treats a hardcoded index as a fallback for when the
    item's cache options are unavailable and resolves "Drop" by NAME first
    (``JBotWithUsV2 skilling-core/.../GatherScript.java:62-66,243-246``), where
    the documented example is ``7`` for logs. Callers that know the item's
    option list should find the label in it rather than hardcoding a number
    here.
    """
    if option < 1:
        raise ValueError(f"backpack option must be 1-based, got {option}")
    if slot < 0:
        raise ValueError(f"backpack slot must be 0-based and non-negative, got {slot}")
    return component_click(BACKPACK_IFACE, BACKPACK_COMPONENT, option=option, sub_slot=slot)
