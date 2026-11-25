"""Component ops, children and ``interact(option_name)``.

``get_component_ops`` replies use the handler's keys (mask, ops[{op, name, transmits}]) and the
live shape PUBLIC_PROTOCOL.md 6.7.2 records for a backpack item: ops 1, 2, 7, 8 and Examine 10,
with op numbers that are NOT the list positions, so a wrapper that clicks ``index + 1`` sends a
different op than the one named.
"""
import pytest

from botwithus.api.components import ComponentOp, ComponentOps, pack, unpack
from botwithus.core.rpc import MalformedReply, RpcError

from fake_agent_reads import RecordingAgent, game_with

COMPONENT = 57
BACKPACK = (1473, 5)
# bits 1, 2, 7, 8, 10 transmit; bit 24 (a list selection transmits) is a non-op flag.
_MASK = (1 << 1) | (1 << 2) | (1 << 7) | (1 << 8) | (1 << 10) | (1 << 24)
_FOOD_OPS = {"mask": _MASK, "ops": [
    {"op": 1, "name": "Eat", "transmits": True},
    {"op": 2, "name": "Drop all", "transmits": True},
    {"op": 7, "name": "Use on", "transmits": False},
    {"op": 8, "name": "Drop", "transmits": True},
    {"op": 10, "name": "Examine", "transmits": True},
]}


def test_component_ops_sends_sub_and_decodes_every_op():
    # Failure: sub not sent (the container's ops come back instead of the slot's), or an op's
    # number, name or transmits flag taken from a neighbour.
    agent = RecordingAgent({"get_component_ops": _FOOD_OPS})
    ops = game_with(agent).component_ops(*BACKPACK, sub=12)
    assert agent.calls == [("get_component_ops", {"iface": 1473, "comp": 5, "sub": 12})]
    assert ops.mask == _MASK
    assert ops.ops[2] == ComponentOp(7, "Use on", False)
    assert ops.names() == ["Eat", "Drop all", "Use on", "Drop", "Examine"]


def test_component_ops_default_sub_is_minus_one():
    agent = RecordingAgent({"get_component_ops": {"mask": 0, "ops": []}})
    assert game_with(agent).component_ops(548, 31).ops == ()
    assert agent.calls == [("get_component_ops", {"iface": 548, "comp": 31, "sub": -1})]


def test_op_named_is_exact_and_case_insensitive():
    ops = ComponentOps.from_reply(_FOOD_OPS)
    assert ops.op_named("drop") == 8          # not 2: "Drop all" is a different op
    assert ops.op_named("  DROP   ALL ") == 2
    assert ops.op_named("Dro") is None


@pytest.mark.parametrize("reply", [
    {"ops": []}, {"mask": 1, "ops": [{"op": 1, "name": "Eat"}]}, [], {"mask": 1, "ops": None},
])
def test_component_ops_malformed(reply):
    with pytest.raises(MalformedReply):
        ComponentOps.from_reply(reply)


def test_interact_queues_the_named_ops_number_on_the_slot():
    # Failure: param1 = list position (4) instead of op 8, param2 not the sub, or param3 not
    # the packed container.
    agent = RecordingAgent({"get_component_ops": _FOOD_OPS})
    game = game_with(agent)
    assert game.interface_component(*BACKPACK, sub=12).interact("Drop") is True
    assert agent.queued() == [(COMPONENT, 8, 12, (1473 << 16) | 5)]


def test_interact_unknown_option_queues_nothing():
    agent = RecordingAgent({"get_component_ops": _FOOD_OPS})
    assert game_with(agent).interface_component(*BACKPACK, sub=0).interact("Wield") is False
    assert agent.methods() == ["get_component_ops"]


def test_interact_component_answers_false_when_not_open():
    agent = RecordingAgent({"get_component_ops": "__error__:get_component_ops: not_found"})
    assert game_with(agent).interact_component(*BACKPACK, "Eat", sub=3) is False
    assert agent.queued() == []


def test_interact_component_raises_on_not_ready():
    # not_ready is not "not open": hiding it as False would make a slow game thread look like
    # a closed interface.
    agent = RecordingAgent({"get_component_ops": "__error__:get_component_ops: not_ready"})
    with pytest.raises(RpcError):
        game_with(agent).interact_component(*BACKPACK, "Eat", sub=3)


def test_handle_read_sends_only_iface_and_comp():
    # get_component does not take sub: sending it is a key the agent never reads.
    agent = RecordingAgent({"get_component": {"iface": 1473, "comp": 5, "text": "x"}})
    found = game_with(agent).interface_component(*BACKPACK, sub=9).read()
    assert agent.calls == [("get_component", {"iface": 1473, "comp": 5})]
    assert found["text"] == "x"


def test_handle_read_not_found_is_none():
    agent = RecordingAgent({"get_component": {"iface": -1, "comp": 5}})
    assert game_with(agent).interface_component(1, 2).read() is None


def test_interact_op_validates_and_queues():
    agent = RecordingAgent()
    handle = game_with(agent).interface_component(1477, 25)
    handle.interact_op(3)
    assert agent.queued() == [(COMPONENT, 3, -1, (1477 << 16) | 25)]
    for bad in (0, 11, True):
        with pytest.raises(ValueError):
            handle.interact_op(bad)


@pytest.mark.parametrize("iface, comp, sub", [(-1, 0, -1), (0x10000, 0, -1), (1, 0, -2),
                                              (1, 0x10000, -1), (1, 0, 0x10000)])
def test_out_of_range_ids_raise_unsent(iface, comp, sub):
    agent = RecordingAgent({"get_component_ops": _FOOD_OPS})
    with pytest.raises(ValueError):
        game_with(agent).component_ops(iface, comp, sub)
    assert agent.calls == []


def test_static_and_dynamic_children():
    agent = RecordingAgent({"get_static_children": {"children": [0, 4, 9, -1]},
                            "get_dynamic_children": {"children": [5, 5, 5]}})
    game = game_with(agent)
    assert game.static_children(1188, 0) == [0, 4, 9, -1]
    assert game.dynamic_children(1473, 5) == [5, 5, 5]
    assert agent.calls == [("get_static_children", {"iface": 1188, "comp": 0}),
                           ("get_dynamic_children", {"iface": 1473, "comp": 5})]


@pytest.mark.parametrize("reply", [{"kids": []}, {"children": None}, {"children": ["1"]}])
def test_children_malformed(reply):
    agent = RecordingAgent({"get_static_children": reply})
    with pytest.raises(MalformedReply):
        game_with(agent).static_children(1, 2)


def test_pack_unpack_round_trip():
    assert pack(1473, 5) == 0x05C10005
    assert unpack(0x05C10005) == (1473, 5)
