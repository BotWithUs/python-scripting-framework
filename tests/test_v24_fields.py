"""Wire v24 fields, decoded end to end through SnapshotView, plus the v24 RPC surface.

Bytes go in at LITERAL offsets from NXTLibrary's SharedLayout.h (the v25 positions of these v24
fields: the fields did not change, but npcs[] and players[] moved and the player stride grew),
never through ``layout``: a test that wrote through the reader's own constants would pass
whatever they said.
"""
import struct

import pytest

from botwithus.api import entities as E
from botwithus.api.game import GameApi
from botwithus.api.snapshot_types import AppearanceSlot, PlayerAppearance, PlayerEntry
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient, RpcError
from botwithus.core.snapshot import SnapshotView
from fakes import FakeNative, build_snapshot

GAME_STATE, STATE_LOBBY = 8, 20
SELF, RUN_ENERGY, RUN_WEIGHT = 24, 552, 556
NPC_COUNT, NPCS, NPC_STRIDE, NPC_TYPE, NPC_ACTIVE, NPC_COMBAT = 592, 596, 48, 4, 40, 44
PLAYER_COUNT, PLAYERS, PLAYER_STRIDE, PL_FLAGS, PL_NAME = 49_748, 49_752, 56, 9, 32
FLAG_MOVING, FLAG_NAME_TRUNCATED = 0x1, 0x2
NBSP = " "


def _players(names: list[tuple[bytes, int]]) -> list[PlayerEntry]:
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, PLAYER_COUNT, len(names))
    for i, (raw, flags) in enumerate(names):
        row = PLAYERS + i * PLAYER_STRIDE
        struct.pack_into("<i", buf, row, 100 + i)
        buf[row + PL_FLAGS] = flags
        buf[row + PL_NAME:row + PL_NAME + len(raw)] = raw
    return SnapshotView(bytes(buf)).players()


def test_player_names_decode_raw_including_nbsp_and_empty():
    rows = _players([
        (("Sir" + NBSP + "Test").encode() + b"\0", 0),
        (b"", 0),                                   # appearance not decoded yet
        (b"Zezima\0", FLAG_MOVING),
    ])
    assert [p.name for p in rows] == ["Sir" + NBSP + "Test", "", "Zezima"]
    assert [p.name_truncated for p in rows] == [False, False, False]
    assert rows[2].moving                           # bit 0 still means moving


def test_truncated_flag_and_unterminated_name_are_bounded():
    # 16 bytes and no NUL breaks the producer's promise; the reader stops at 15 bytes and
    # never reads the next row (whose server index sits right after the field).
    rows = _players([(b"ABCDEFGHIJKLMNOP", FLAG_NAME_TRUNCATED), (b"Next\0", 0)])
    assert rows[0].name == "ABCDEFGHIJKLMNO"
    assert rows[0].name_truncated and not rows[0].moving
    assert rows[1].name == "Next"


def test_npc_active_type_and_combat_level():
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, NPC_COUNT, 3)
    for i, (base, active, combat) in enumerate([(9633, 11476, 41), (32248, -1, 0),
                                                (3, 3, 2)]):
        row = NPCS + i * NPC_STRIDE
        struct.pack_into("<ii", buf, row, 50 + i, base)
        struct.pack_into("<ii", buf, row + NPC_ACTIVE, active, combat)
    npcs = SnapshotView(bytes(buf)).npcs()
    assert [(n.type_id, n.active_type_id, n.combat_level) for n in npcs] == [
        (9633, 11476, 41),     # Xenia: base differs from the morph-resolved type
        (32248, -1, 0),        # morph resolves to nothing: -1, never replaced by type_id
        (3, 3, 2),             # no morph: active == base
    ]
    assert [n.is_morph_hidden for n in npcs] == [False, True, False]


def _self_run(game_state: int, energy: int, weight: int):
    buf = bytearray(build_snapshot(game_state=game_state))
    struct.pack_into("<ii", buf, SELF + RUN_ENERGY, energy, weight)
    return SnapshotView(bytes(buf)).self_player


def test_run_energy_and_weight_in_world():
    me = _self_run(30, 73, 12)
    assert (me.run_energy, me.run_weight) == (73, 12)


def test_run_fields_unreadable_in_world_stay_minus_one():
    me = _self_run(30, -1, -1)
    assert (me.run_energy, me.run_weight) == (-1, -1)


def test_run_fields_out_of_world_read_unknown_not_zero():
    me = _self_run(STATE_LOBBY, 0, 0)
    assert (me.run_energy, me.run_weight) == (-1, -1)


def _player(name: str) -> PlayerEntry:
    return PlayerEntry(1, 0, 0, 0, False, -1, -1, -1, 3, -1, name=name)


def test_names_compare_with_nbsp_normalised_on_both_sides():
    assert E.names_equal("Sir" + NBSP + "Test", "Sir Test")
    assert E.names_equal("Sir Test", "Sir" + NBSP + "Test")
    assert E.names_equal("a" + NBSP + "b", "a" + NBSP + "b")
    assert not E.names_equal("Sir Test", "SirTest")
    assert E.normalize_name("x" + NBSP + "y") == "x y"


def test_empty_name_never_matches():
    assert not E.names_equal("", "")
    assert not E.names_equal("", "Zezima")
    assert not E.names_equal("Zezima", "")


def test_by_name_filter():
    query = E.EntityQuery([_player("Sir" + NBSP + "Test"), _player(""), _player("Zezima")],
                          lambda e: e.tile)
    assert [p.name for p in E.by_name(query, "Sir Test").all()] == ["Sir" + NBSP + "Test"]
    assert E.by_name(query, "").count() == 0       # an undecoded name is never a match


_APPEARANCE = {
    "render_id": 1426, "npc_transform_id": -1, "gender": 0,
    "slots": [{"kit": i, "item": -1} for i in range(15)] + [{"kit": -1, "item": 841}]
             + [{"kit": i, "item": -1} for i in range(16, 19)],
    "colours": [3, 16, 16, 0, 0, 0, 0, 0, 0, 0],
}


def _game(responder) -> GameApi:
    native_mod.set_backend(FakeNative(responder=responder))
    return GameApi(RpcClient(NativeHost.attach(4242)), lambda: None)


def test_player_appearance_sends_server_index_and_decodes_reply():
    seen = {}

    def responder(method, params):
        seen["call"] = (method, params)
        return _APPEARANCE

    got = _game(responder).player_appearance(7)
    assert seen["call"] == ("get_player_appearance", {"serverIndex": 7})
    assert (got.render_id, got.npc_transform_id, got.gender) == (1426, -1, 0)
    assert len(got.slots) == 19 and len(got.colours) == 10
    assert got.slots[15] == AppearanceSlot(-1, 841)    # client order, not wearpos 3
    assert got.worn_item_ids() == (841,)


def test_player_appearance_empty_colours_is_legal():
    got = PlayerAppearance.from_reply({**_APPEARANCE, "colours": []})
    assert got.colours == ()


def test_player_appearance_refusal_raises_with_the_agent_code():
    game = _game(lambda m, p: "__error__:appearance_not_loaded")
    with pytest.raises(RpcError) as ei:
        game.player_appearance(9)
    assert ei.value.agent_message == "appearance_not_loaded"


def test_player_appearance_missing_key_is_refused():
    reply = dict(_APPEARANCE)
    del reply["slots"]
    with pytest.raises(KeyError):
        PlayerAppearance.from_reply(reply)


# A v24 component map as get_component returns it (NXTLibrary PR #41, 33 keys). The run-orb
# text that used to read hidden=1 while on screen is the case v24 fixed.
_V24_COMPONENT = {
    "iface": 1465, "comp": 20, "hidden": 0, "visible": True, "visible_global": True,
    "slot_flags": 0, "parent_global": (1465 << 16) | 3, "scrollable": False,
    "scroll_x": 0, "scroll_y": 0, "scroll_w": 0, "scroll_h": 0,
}


def test_component_dict_passes_v24_visibility_and_scroll_keys_through():
    comp = _game(lambda m, p: _V24_COMPONENT if m == "get_component" else {}).component(1465, 20)
    for key in ("visible", "visible_global", "slot_flags", "parent_global", "scrollable",
                "scroll_x", "scroll_y", "scroll_w", "scroll_h"):
        assert comp[key] == _V24_COMPONENT[key], key
    assert comp["hidden"] == 0 and comp["visible"] is True


def test_component_layer_scroll_keys_pass_through():
    layer = {**_V24_COMPONENT, "scrollable": True, "scroll_y": 120, "scroll_h": 900}
    comp = _game(lambda m, p: layer).component(1465, 3)
    assert (comp["scrollable"], comp["scroll_y"], comp["scroll_h"]) == (True, 120, 900)
