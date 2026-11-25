"""Inventory containers: clicks by slot, by item id and by item name, over a real snapshot buffer.

Slot grids mirror the Java host (Backpack 1473:5, Bank 517:201, Equipment 1464:15). A click is
the COMPONENT action (57) with the op in param1, the slot in param2 and the packed grid in
param3, as ``InventoryContainer.interact`` sends it there.
"""
import struct

import pytest

from botwithus.core import layout as L
from botwithus.core.snapshot import SnapshotView

from fake_agent_reads import RecordingAgent, game_with
from fakes import build_snapshot

COMPONENT = 57
LOGS, OAK_LOGS, KNIFE, COINS = 1511, 1521, 946, 995
NAMES = {LOGS: "Logs", OAK_LOGS: "Oak logs", KNIFE: "Knife", COINS: "Coins"}.get

_DROP_OPS = {"mask": (1 << 1) | (1 << 8), "ops": [
    {"op": 1, "name": "Craft", "transmits": True},
    {"op": 8, "name": "Drop", "transmits": True},
]}


def _snapshot(*inventories) -> SnapshotView:
    """``(inv_id, slot_count, {slot: (item_id, qty)})`` written at the layout's offsets."""
    out = bytearray(build_snapshot())
    struct.pack_into("<I", out, L.SNAP_INVENTORY_COUNT, len(inventories))
    first = 0
    for index, (inv_id, slot_count, filled) in enumerate(inventories):
        header = L.SNAP_INVENTORIES + index * L.SZ_INVENTORY_HEADER
        struct.pack_into("<iHH", out, header, inv_id, slot_count, first)
        for slot in range(slot_count):
            item_id, quantity = filled.get(slot, (-1, 0))
            struct.pack_into("<ii", out, L.SNAP_INV_ITEMS + (first + slot) * L.SZ_INVENTORY_ITEM,
                             item_id, quantity)
        first += slot_count
    struct.pack_into("<I", out, L.SNAP_INV_ITEM_COUNT, first)
    return SnapshotView(bytes(out))


BACKPACK = (93, 28, {0: (KNIFE, 1), 4: (LOGS, 1), 9: (OAK_LOGS, 1), 12: (LOGS, 1),
                     27: (COINS, 75_000)})
WORN = (94, 19, {3: (1381, 1)})


def _game(agent, *inventories):
    view = _snapshot(*inventories)
    return game_with(agent, lambda: view)


def test_interact_by_slot_and_op_number():
    # Failure: the slot in param1 and the op in param2 (swapped), or the wrong grid.
    agent = RecordingAgent()
    assert _game(agent, BACKPACK).backpack().interact(4, 8) is True
    assert agent.queued() == [(COMPONENT, 8, 4, (1473 << 16) | 5)]


def test_interact_by_option_name_reads_the_slots_live_ops():
    agent = RecordingAgent({"get_component_ops": _DROP_OPS})
    assert _game(agent, BACKPACK).backpack().interact(9, "drop") is True
    assert agent.calls[0] == ("get_component_ops", {"iface": 1473, "comp": 5, "sub": 9})
    assert agent.queued() == [(COMPONENT, 8, 9, (1473 << 16) | 5)]


def test_interact_item_clicks_the_first_slot_holding_it():
    agent = RecordingAgent()
    assert _game(agent, BACKPACK).backpack().interact_item(LOGS, 1) is True
    assert agent.queued() == [(COMPONENT, 1, 4, (1473 << 16) | 5)]


def test_interact_named_uses_the_resolver_and_substring():
    agent = RecordingAgent({"get_component_ops": _DROP_OPS})
    assert _game(agent, BACKPACK).backpack().interact_named("oak", "Drop", names=NAMES) is True
    assert agent.queued() == [(COMPONENT, 8, 9, (1473 << 16) | 5)]


@pytest.mark.parametrize("call", [
    lambda c: c.interact(28, 1),             # past the 28-slot grid
    lambda c: c.interact(-1, 1),
    lambda c: c.interact_item(4151, 1),      # not held
    lambda c: c.interact_named("rune", 1, names=NAMES),
])
def test_nothing_to_click_is_false_and_sends_nothing(call):
    agent = RecordingAgent({"get_component_ops": _DROP_OPS})
    assert call(_game(agent, BACKPACK).backpack()) is False
    assert agent.calls == []


def test_unknown_option_name_is_false_and_queues_nothing():
    agent = RecordingAgent({"get_component_ops": _DROP_OPS})
    assert _game(agent, BACKPACK).backpack().interact(4, "Eat") is False
    assert agent.methods() == ["get_component_ops"]


def test_closed_grid_is_false_not_an_error():
    agent = RecordingAgent({"get_component_ops": "__error__:get_component_ops: not_found"})
    assert _game(agent, BACKPACK).backpack().interact(4, "Drop") is False
    assert agent.queued() == []


def test_bank_not_published_is_false_and_reads_absent():
    agent = RecordingAgent()
    bank = _game(agent, BACKPACK).bank()
    assert bank.view().present is False
    assert bank.interact(0, 1) is False
    assert agent.calls == []


def test_bank_and_equipment_click_their_own_grids():
    agent = RecordingAgent()
    game = _game(agent, BACKPACK, WORN, (95, 400, {399: (LOGS, 5000)}))
    assert game.bank().interact(399, 7) is True
    assert game.equipment().interact(3, 1) is True
    assert agent.queued() == [(COMPONENT, 7, 399, (517 << 16) | 201),
                              (COMPONENT, 1, 3, (1464 << 16) | 15)]


def test_count_and_contains_by_name():
    # Failure: an exact-match test ("logs" would miss "Oak logs"), or a count of slots
    # rather than of quantity.
    pack = _game(RecordingAgent(), BACKPACK).backpack()
    assert pack.count_named("logs", names=NAMES) == 3
    assert pack.contains_named("COIN", names=NAMES) is True
    assert pack.contains_named("rune", names=NAMES) is False
    assert pack.view().count_named("coins", names=NAMES) == 75_000


def test_a_resolver_that_is_not_callable_says_why():
    # names=None is the cache's item names now (test_n2_definitions); anything else that is
    # not callable is the caller's bug.
    pack = _game(RecordingAgent(), BACKPACK).backpack()
    with pytest.raises(TypeError, match="callable"):
        pack.count_named("logs", names="not a resolver")


def test_unknown_names_never_match():
    pack = _game(RecordingAgent(), BACKPACK).backpack()
    assert pack.count_named("logs", names=lambda item_id: None) == 0


def test_container_reads_follow_the_current_snapshot():
    frames = [_snapshot(BACKPACK), _snapshot((93, 28, {}))]
    game = game_with(RecordingAgent(), lambda: frames[0])
    pack = game.backpack()
    assert pack.count(LOGS) == 2
    frames.pop(0)
    assert pack.count(LOGS) == 0 and pack.free_slots() == 28


def test_container_obj_vars_reads_the_slot():
    agent = RecordingAgent({"get_obj_vars": {"slots": [{"slot": 4, "vars": [
        {"id": 2001, "value": 12}]}]}})
    assert _game(agent, BACKPACK).backpack().obj_vars(4) == {2001: 12}
    assert agent.calls == [("get_obj_vars", {"inv_id": 93, "slot": 4})]


def test_bad_option_type_raises():
    with pytest.raises(ValueError):
        _game(RecordingAgent(), BACKPACK).backpack().interact(4, 0)
