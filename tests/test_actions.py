"""Entity action builders: the param shapes, and the slots they refuse.

Every expectation here is pinned against the shapes documented in
``api/actions.py`` (the OBJECT / NPC / GROUND_ITEM / COMPONENT comments), not
against whatever the builders happen to emit.
"""
import pytest

from botwithus.api.actions import (
    BACKPACK_COMPONENT,
    BACKPACK_IFACE,
    ActionType,
    GameAction,
    backpack_action,
    ground_item_action,
    loc_base_id,
    npc_action,
    object_action,
)
from botwithus.api.snapshot_types import GroundItem, LocationEntry, NpcEntry

FLAG_COMBINED_SECTION = 0x2


def make_loc(*, type_id=1234, interact_id=5678, flags=0, resolved_id=9999,
             tile_x=3200, tile_y=3201) -> LocationEntry:
    return LocationEntry(type_id=type_id, interact_id=interact_id, animation_id=-1,
                         tile_x=tile_x, tile_y=tile_y, plane=0, shape=10, rotation=0,
                         flags=flags, resolved_id=resolved_id)


def make_npc(*, server_index=42, type_id=7, tile_x=3300, tile_y=3301) -> NpcEntry:
    return NpcEntry(server_index=server_index, type_id=type_id, tile_x=tile_x, tile_y=tile_y,
                    plane=0, moving=False, following_index=-1, animation_id=-1,
                    stance_id=0, hp=100, max_hp=100, spot_anim_id=-1)


def make_ground_item(*, item_id=1511, quantity=1, tile_x=3210, tile_y=3211) -> GroundItem:
    return GroundItem(item_id=item_id, quantity=quantity, tile_x=tile_x, tile_y=tile_y, plane=0)


# --- object ----------------------------------------------------------------

def test_object_action_uses_interact_id_and_tile():
    loc = make_loc(type_id=1234, interact_id=5678, tile_x=3200, tile_y=3201)
    assert object_action(loc, 1) == GameAction(ActionType.OBJECT1, 5678, 3200, 3201)


def test_object_action_on_a_combined_section_uses_type_id():
    """A section carries its base id in type_id; its interact_id is always -1."""
    loc = make_loc(type_id=1234, interact_id=-1, flags=FLAG_COMBINED_SECTION)
    assert loc.is_combined_section
    assert object_action(loc, 1).param1 == 1234


def test_object_action_never_uses_resolved_id():
    """resolved_id is for name/option lookup only -- addressing it would be wrong."""
    loc = make_loc(interact_id=5678, resolved_id=9999)
    assert object_action(loc, 3).param1 == 5678


def test_object_action_maps_every_slot_to_its_action_id():
    loc = make_loc()
    expected = [ActionType.OBJECT1, ActionType.OBJECT2, ActionType.OBJECT3,
                ActionType.OBJECT4, ActionType.OBJECT5, ActionType.OBJECT6]
    assert [object_action(loc, s).action_id for s in range(1, 7)] == expected


def test_loc_base_id_matches_the_documented_rule():
    assert loc_base_id(make_loc(type_id=11, interact_id=22)) == 22
    assert loc_base_id(make_loc(type_id=11, interact_id=-1, flags=FLAG_COMBINED_SECTION)) == 11


# --- npc -------------------------------------------------------------------

def test_npc_action_addresses_the_server_index_not_the_tile():
    npc = make_npc(server_index=42, tile_x=3300, tile_y=3301)
    assert npc_action(npc, 1) == GameAction(ActionType.NPC1, 42, 0, 0)


def test_npc_action_maps_every_slot_to_its_action_id():
    npc = make_npc()
    expected = [ActionType.NPC1, ActionType.NPC2, ActionType.NPC3,
                ActionType.NPC4, ActionType.NPC5, ActionType.NPC6]
    assert [npc_action(npc, s).action_id for s in range(1, 7)] == expected


# --- ground item -----------------------------------------------------------

def test_ground_item_action_uses_item_id_and_tile():
    gi = make_ground_item(item_id=1511, tile_x=3210, tile_y=3211)
    assert ground_item_action(gi, 2) == GameAction(ActionType.GROUND_ITEM2, 1511, 3210, 3211)


def test_ground_item_action_maps_every_slot_to_its_action_id():
    gi = make_ground_item()
    expected = [ActionType.GROUND_ITEM1, ActionType.GROUND_ITEM2, ActionType.GROUND_ITEM3,
                ActionType.GROUND_ITEM4, ActionType.GROUND_ITEM5, ActionType.GROUND_ITEM6]
    assert [ground_item_action(gi, s).action_id for s in range(1, 7)] == expected


# --- slot validation -------------------------------------------------------

@pytest.mark.parametrize("builder, entity", [
    (object_action, make_loc()),
    (npc_action, make_npc()),
    (ground_item_action, make_ground_item()),
])
@pytest.mark.parametrize("slot", [-1, 0, 7, 99])
def test_entity_builders_reject_out_of_range_slots(builder, entity, slot):
    """0 is the one that matters: [slot - 1] would silently select slot 6."""
    with pytest.raises(ValueError):
        builder(entity, slot)


@pytest.mark.parametrize("helper, limit", [
    (ActionType.object_option, 6),
    (ActionType.npc_option, 6),
    (ActionType.ground_item_option, 6),
    (ActionType.player_option, 10),
])
@pytest.mark.parametrize("slot", [-1, 0])
def test_option_helpers_raise_instead_of_wrapping(helper, limit, slot):
    """Slot 0 used to return the LAST option id, and -1 the one before it.

    Python's negative indexing made an impossible slot dispatch a real menu
    entry. player_option was arithmetic, so it returned 43 for slot 0 -- not a
    player option at all.
    """
    with pytest.raises(ValueError):
        helper(slot)


@pytest.mark.parametrize("helper, first_invalid", [
    (ActionType.object_option, 7),
    (ActionType.npc_option, 7),
    (ActionType.ground_item_option, 7),
    (ActionType.player_option, 11),
])
def test_option_helpers_reject_one_past_the_end(helper, first_invalid):
    with pytest.raises(ValueError):
        helper(first_invalid)


def test_player_option_still_covers_its_full_range():
    assert [ActionType.player_option(s) for s in (1, 10)] == [ActionType.PLAYER1,
                                                              ActionType.PLAYER1 + 9]


# --- backpack --------------------------------------------------------------

def test_backpack_action_shape():
    """COMPONENT: param1=1-based option, param2=sub-slot, param3=(iface<<16)|comp."""
    action = backpack_action(slot=3, option=7)
    assert action == GameAction(ActionType.COMPONENT, 7, 3,
                                (BACKPACK_IFACE << 16) | BACKPACK_COMPONENT)


def test_backpack_action_targets_the_documented_grid():
    assert (BACKPACK_IFACE, BACKPACK_COMPONENT) == (1473, 5)


def test_backpack_action_keeps_slot_zero_usable():
    """Backpack slots are 0-based, unlike the 1-based menu option."""
    assert backpack_action(slot=0, option=1).param2 == 0


@pytest.mark.parametrize("slot, option", [(0, 0), (0, -1), (-1, 1)])
def test_backpack_action_rejects_impossible_slot_or_option(slot, option):
    with pytest.raises(ValueError):
        backpack_action(slot=slot, option=option)
