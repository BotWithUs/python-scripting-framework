"""ObjVarChange decode (event 13) and the RPC-only reads: obj vars, world spot anims, action
history and last action time.

Event bodies are packed at LITERAL offsets from native-scripting-host src/wire/Events.h
(``ObjVarChangeBody``: int32 invId, slot, varId, oldValue, newValue), never through the
decoder's own struct. Every value is distinct and non-default, so a swapped or shifted field
reads as a wrong number rather than passing by coincidence.
"""
import struct

import pytest

from botwithus.api import events as E
from botwithus.api.rpc_types import ActionHistoryEntry, SlotObjVars, WorldSpotAnim
from botwithus.api.snapshot_types import Tile
from botwithus.core import native as native_mod
from botwithus.core.events import EventRingReader, decode_event
from botwithus.core.native import NativeHost
from botwithus.core.rpc import MalformedReply, RpcError

from fake_agent_reads import RecordingAgent, game_with
from fakes import FakeNative, build_events

EVT_OBJ_VAR_CHANGE = 13  # Events.h kEventObjVarChange


def _obj_var_body(inv_id, slot, var_id, old, new) -> bytes:
    body = bytearray(20)
    struct.pack_into("<i", body, 0, inv_id)
    struct.pack_into("<i", body, 4, slot)
    struct.pack_into("<i", body, 8, var_id)
    struct.pack_into("<i", body, 12, old)
    struct.pack_into("<i", body, 16, new)
    return bytes(body)


# --- ObjVarChange -------------------------------------------------------------------------
# Failure would be: None (the event skipped, as before this change), or any field holding
# another field's value.

def test_obj_var_change_decodes_every_field_through_the_ring():
    native_mod.set_backend(FakeNative(events=build_events([
        (EVT_OBJ_VAR_CHANGE, _obj_var_body(93, 17, 4321, -5, 123_456)),
    ])))
    (evt,) = EventRingReader(NativeHost.attach(4242)).poll()
    assert evt == E.ObjVarChanged(0, inv_id=93, slot=17, var_id=4321, old_value=-5,
                                  new_value=123_456)


def test_obj_var_change_short_body_is_skipped_not_guessed():
    assert decode_event(EVT_OBJ_VAR_CHANGE, _obj_var_body(93, 1, 2, 3, 4)[:19], 0) is None


def test_obj_var_change_ignores_bytes_past_the_body():
    evt = decode_event(EVT_OBJ_VAR_CHANGE, _obj_var_body(95, 2, 30, 40, 50) + b"\xff" * 4, 9)
    assert (evt.seq, evt.inv_id, evt.slot, evt.var_id, evt.old_value, evt.new_value) == \
        (9, 95, 2, 30, 40, 50)


# --- obj vars (get_obj_vars) --------------------------------------------------------------

_OBJ_VARS = {"slots": [
    {"slot": 3, "vars": [{"id": 7001, "value": -42}, {"id": 7002, "value": 99}]},
    {"slot": 11, "vars": [{"id": 8000, "value": 2_000_000}]},
]}


def test_obj_vars_sends_inv_id_only_and_decodes_every_slot():
    # Failure: a "slot" key sent (the agent would read only that slot), or a value under
    # another var id / slot.
    agent = RecordingAgent({"get_obj_vars": _OBJ_VARS})
    got = game_with(agent).obj_vars(93)
    assert agent.calls == [("get_obj_vars", {"inv_id": 93})]
    assert [s.slot for s in got] == [3, 11]
    assert dict(got[0].vars) == {7001: -42, 7002: 99}
    assert got[1].get(8000) == 2_000_000
    assert isinstance(got[0], SlotObjVars)


def test_slot_obj_vars_sends_the_slot_and_returns_that_slots_map():
    agent = RecordingAgent({"get_obj_vars": {"slots": [_OBJ_VARS["slots"][1]]}})
    assert game_with(agent).slot_obj_vars(94, 11) == {8000: 2_000_000}
    assert agent.calls == [("get_obj_vars", {"inv_id": 94, "slot": 11})]


def test_slot_obj_vars_empty_reply_is_an_empty_map():
    agent = RecordingAgent({"get_obj_vars": {"slots": []}})
    assert game_with(agent).slot_obj_vars(93, 4) == {}


@pytest.mark.parametrize("inv_id, slot", [(-1, None), (93, -1), (True, None), (93, 1.0)])
def test_obj_vars_refuses_a_bad_id_unsent(inv_id, slot):
    # Failure: a call reaching the agent; it would read slot -1 as "every slot".
    agent = RecordingAgent({"get_obj_vars": _OBJ_VARS})
    with pytest.raises(ValueError):
        game_with(agent).obj_vars(inv_id, slot)
    assert agent.calls == []


def test_obj_vars_malformed_reply_raises():
    agent = RecordingAgent({"get_obj_vars": {"slots": [{"slot": 1}]}})
    with pytest.raises(MalformedReply):
        game_with(agent).obj_vars(93)


# --- world spot anims (query_spot_anims) --------------------------------------------------

def test_spot_anims_decode_id_and_tile():
    # Failure: tile_x / tile_y swapped, or the id read from a tile key.
    agent = RecordingAgent({"query_spot_anims": {"spot_anims": [
        {"id": 2918, "tile_x": 3222, "tile_y": 3218},
        {"id": 86, "tile_x": 2964, "tile_y": 3378},
    ]}})
    got = game_with(agent).spot_anims()
    assert agent.calls == [("query_spot_anims", {})]
    assert got == (WorldSpotAnim(2918, 3222, 3218), WorldSpotAnim(86, 2964, 3378))
    assert got[0].tile == Tile(3222, 3218, -1)


def test_spot_anims_missing_list_is_malformed():
    agent = RecordingAgent({"query_spot_anims": {"anims": []}})
    with pytest.raises(MalformedReply):
        game_with(agent).spot_anims()


# --- action history -----------------------------------------------------------------------

_HISTORY = [
    {"action_id": 57, "param1": 8, "param2": 4, "param3": (1473 << 16) | 5,
     "timestamp": 90_000_100, "delta": 0},
    {"action_id": 23, "param1": 1, "param2": 3200, "param3": 3201,
     "timestamp": 90_000_700, "delta": 600},
]


def test_action_history_is_a_top_level_array_in_agent_order():
    # Failure: reading it as a {"history": ...} map, reordering it, or a param in another slot.
    agent = RecordingAgent({"get_action_history": _HISTORY})
    got = game_with(agent).action_history()
    assert agent.calls == [("get_action_history", {"max_results": 128})]
    assert got == (ActionHistoryEntry(57, 8, 4, (1473 << 16) | 5, 90_000_100, 0),
                   ActionHistoryEntry(23, 1, 3200, 3201, 90_000_700, 600))


def test_action_history_sends_the_filter_and_the_cap():
    agent = RecordingAgent({"get_action_history": []})
    assert game_with(agent).action_history(5, action_id=57) == ()
    assert agent.calls == [("get_action_history", {"max_results": 5, "action_id_filter": 57})]


@pytest.mark.parametrize("kwargs", [{"max_results": 0}, {"max_results": 129},
                                    {"action_id": -1}, {"max_results": True}])
def test_action_history_refuses_what_the_agent_would_clamp_or_ignore(kwargs):
    agent = RecordingAgent({"get_action_history": []})
    with pytest.raises(ValueError):
        game_with(agent).action_history(**kwargs)
    assert agent.calls == []


def test_last_action_time_zero_means_none_dispatched():
    agent = RecordingAgent({"get_last_action_time": {"timestamp": 0}})
    assert game_with(agent).last_action_time() is None


def test_last_action_time_passes_the_agent_clock_through():
    agent = RecordingAgent({"get_last_action_time": {"timestamp": 90_000_700}})
    assert game_with(agent).last_action_time() == 90_000_700


def test_last_action_time_malformed():
    agent = RecordingAgent({"get_last_action_time": {"time": 5}})
    with pytest.raises(MalformedReply):
        game_with(agent).last_action_time()


def test_an_agent_error_surfaces_as_rpc_error():
    agent = RecordingAgent({"query_spot_anims": "__error__:query_spot_anims: boom"})
    with pytest.raises(RpcError) as info:
        game_with(agent).spot_anims()
    assert info.value.agent_message == "query_spot_anims: boom"
