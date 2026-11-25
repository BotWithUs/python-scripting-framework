"""Wire v25: the server tile, decoded end to end through SnapshotView, plus the v25 RPC surface.

Bytes go in at LITERAL offsets from NXTLibrary's v25 SharedLayout.h (412510a), never through
``layout``: a test that wrote through the reader's own constants would pass whatever they said.
RPC replies are written with the wire's own keys, from docs/PUBLIC_PROTOCOL.md section 6.2.
"""
import ast
import re
import struct
from pathlib import Path

import pytest

import botwithus
from botwithus.api import entities as E
from botwithus.api.game import GameApi
from botwithus.api.snapshot_types import (
    EntityReadStatus,
    Headbar,
    HeadbarList,
    HintArrow,
    HintArrowList,
    OverheadText,
    PlayerEntry,
    Tile,
)
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import MalformedReply, RpcClient, RpcError
from botwithus.core.snapshot import SnapshotView
from fakes import FakeNative, build_snapshot

STATE_LOGIN, STATE_LOBBY, STATE_IN_GAME = 10, 20, 30
SNAPSHOT_SIZE, SLOT_STRIDE = 469_192, 469_248
OWN_INDEX, SELF, SELF_RUN_ENERGY, SELF_SERVER_TILE = 12, 24, 552, 560
PLAYER_COUNT, PLAYERS, PLAYER_STRIDE, PL_SERVER_TILE, PL_TILE = 49_748, 49_752, 56, 48, 4


def _self(game_state: int, server_tile: tuple[int, int, int]):
    buf = bytearray(build_snapshot(game_state=game_state, self_tile=(3200, 3201, 1)))
    struct.pack_into("<hhb", buf, SELF + SELF_SERVER_TILE, *server_tile)
    return SnapshotView(bytes(buf)).self_player


def test_self_server_tile_leads_and_the_visual_tile_stays_put():
    me = _self(STATE_IN_GAME, (3203, 3204, 1))
    assert me.server_tile == Tile(3203, 3204, 1)
    assert me.tile == Tile(3200, 3201, 1)
    assert me.has_server_tile


def test_self_server_tile_unreadable_in_world_stays_minus_one():
    me = _self(STATE_IN_GAME, (-1, -1, -1))
    assert me.server_tile == Tile(-1, -1, -1)
    assert not me.has_server_tile


@pytest.mark.parametrize("state", [STATE_LOGIN, STATE_LOBBY])
def test_self_server_tile_out_of_world_is_unknown_never_tile_zero(state):
    # An older v25 agent zeroes the self block outside the world: (0,0,0) must not pass for a
    # tile. The current agent's -1s are in test_local_player_presence.py.
    me = _self(state, (0, 0, 0))
    assert me.server_tile == Tile(-1, -1, -1)
    assert not me.has_server_tile


def _world_without_a_player() -> SnapshotView:
    """gameState 30 with ownIndex -1 and a zero self block: what an older v25 agent publishes
    when it has no logged-in player (State.cpp, inWorld; PUBLIC_PROTOCOL.md section 3.5)."""
    buf = bytearray(build_snapshot(game_state=STATE_IN_GAME))
    buf[SELF:SELF + 568] = bytes(568)
    struct.pack_into("<i", buf, OWN_INDEX, -1)
    return SnapshotView(bytes(buf))


def test_state_30_with_no_player_reads_no_false_server_tile():
    view = _world_without_a_player()
    assert view.in_game and view.own_index == -1
    assert view.self_player.server_tile == Tile(-1, -1, -1)
    assert not view.self_player.has_server_tile


def test_state_30_with_no_player_reads_no_false_run_fields():
    me = _world_without_a_player().self_player
    assert (me.run_energy, me.run_weight) == (-1, -1)


def test_self_block_is_read_for_the_lowest_own_index():
    # Server index 0 is a real index: the gate is ownIndex -1, not falsy.
    buf = bytearray(build_snapshot(game_state=STATE_IN_GAME))
    struct.pack_into("<i", buf, OWN_INDEX, 0)
    struct.pack_into("<hhb", buf, SELF + SELF_SERVER_TILE, 3203, 3204, 1)
    struct.pack_into("<i", buf, SELF + SELF_RUN_ENERGY, 73)
    me = SnapshotView(bytes(buf)).self_player
    assert me.server_tile == Tile(3203, 3204, 1) and me.run_energy == 73


def test_self_server_tile_plane_is_signed_and_separate():
    assert _self(STATE_IN_GAME, (3203, 3204, 3)).server_plane == 3
    assert _self(STATE_IN_GAME, (3203, 3204, -1)).server_plane == -1
    assert not _self(STATE_IN_GAME, (3203, 3204, -1)).has_server_tile


def _players(rows: list[tuple[int, int, int, int, int]]):
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, PLAYER_COUNT, len(rows))
    for i, (x, y, sx, sy, sp) in enumerate(rows):
        row = PLAYERS + i * PLAYER_STRIDE
        struct.pack_into("<i", buf, row, 7 + i)
        struct.pack_into("<hh", buf, row + PL_TILE, x, y)
        struct.pack_into("<hhb", buf, row + PL_SERVER_TILE, sx, sy, sp)
    return SnapshotView(bytes(buf)).players()


def test_player_rows_decode_the_server_tile_at_the_v25_stride():
    rows = _players([(3200, 3201, 3203, 3204, 1), (10, 11, -1, -1, -1), (3202, 3203, 3202, 3203, 0)])
    assert [p.server_index for p in rows] == [7, 8, 9]       # row 1 and 2: the stride is pinned
    assert rows[0].server_tile == Tile(3203, 3204, 1)
    assert rows[0].tile.x == 3200 and rows[0].has_server_tile
    assert rows[1].server_tile == Tile(-1, -1, -1) and not rows[1].has_server_tile
    assert rows[2].server_tile == Tile(3202, 3203, 0) and rows[2].has_server_tile


def test_a_player_row_built_by_hand_defaults_to_unknown():
    row = PlayerEntry(1, 0, 0, 0, False, -1, -1, -1, 3, -1)
    assert row.server_tile == Tile(-1, -1, -1) and not row.has_server_tile


# --- snapshot stride (sizeof(Snapshot) != slot stride at v25) --------------------------------

def test_frame_size_is_the_struct_not_the_slot_stride():
    assert len(build_snapshot()) == SNAPSHOT_SIZE
    SnapshotView(bytes(SNAPSHOT_SIZE))
    with pytest.raises(ValueError, match="469248 bytes"):
        SnapshotView(bytes(SLOT_STRIDE))


_MAPPING_NAMES = re.compile(r"(HDR|RING|ES)_[A-Z0-9_]+")
_FRAME_SIZE = "SNAPSHOT_SIZE"


def _refers_to(node: ast.AST, name: str) -> bool:
    return any((isinstance(n, ast.Attribute) and n.attr == name)
               or (isinstance(n, ast.Name) and n.id == name)
               for n in ast.walk(node))


def _slot_or_ring_arithmetic(source: str) -> list[str]:
    """What in ``source`` could locate a slot or the ring: any mention of a mapping-header or
    ring constant (attribute, bare name, import, or a string as for ``getattr``), and any import
    of the frame size, or arithmetic on it. Comparing against it, as the frame check does, is
    fine. A name assembled at run time cannot be seen statically."""
    found = []
    for node in ast.walk(ast.parse(source)):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.alias):
            name = node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        if name is not None and _MAPPING_NAMES.fullmatch(name):
            found.append(name)
        if isinstance(node, ast.alias) and node.name == _FRAME_SIZE:
            found.append(f"import {_FRAME_SIZE}")
        if isinstance(node, ast.Constant) and node.value == _FRAME_SIZE:
            found.append(f"string {_FRAME_SIZE}")
        if isinstance(node, (ast.BinOp, ast.AugAssign)) and _refers_to(node, _FRAME_SIZE):
            found.append(f"arithmetic on {_FRAME_SIZE}")
    return found


def test_no_module_computes_a_slot_or_ring_offset():
    """The frontend must take one frame from the native host and locate nothing itself."""
    root = Path(botwithus.__file__).parent
    found = {path.name: hits for path in root.rglob("*.py") if path.name != "layout.py"
             for hits in [_slot_or_ring_arithmetic(path.read_text(encoding="utf-8"))] if hits}
    assert found == {}


@pytest.mark.parametrize("source", [
    "x = L.HDR_SNAPSHOT_OFF0",
    "x = L.RING_SLOTS",
    "x = L.ES_BODY",
    "from .layout import HDR_SNAPSHOT_OFF0",
    "from .layout import RING_SLOTS as slots",
    "x = HDR_RING_OFF",
    'x = getattr(L, "HDR_RING_OFF")',
    "x = 64 + L.SNAPSHOT_SIZE * 1",
    "x = 64 + SNAPSHOT_SIZE",
    "from .layout import SNAPSHOT_SIZE",
    'x = 64 + getattr(L, "SNAPSHOT_SIZE")',
    "x = 64\nx += L.SNAPSHOT_SIZE",
])
def test_the_scan_sees_each_way_of_reaching_the_mapping(source):
    assert _slot_or_ring_arithmetic(source) != []


def test_the_scan_allows_the_frame_length_check():
    assert _slot_or_ring_arithmetic(
        'if len(buf) != L.SNAPSHOT_SIZE:\n    raise ValueError(f"{L.SNAPSHOT_SIZE}")') == []


# --- RPC ---------------------------------------------------------------------------------------

def _game(responder) -> GameApi:
    native_mod.set_backend(FakeNative(responder=responder))
    return GameApi(RpcClient(NativeHost.attach(4242)), lambda: None)


def _recording(reply):
    calls = []

    def responder(method, params):
        calls.append((method, params))
        return reply

    return _game(responder), calls


_TILE_ARROW = {"source": "tree", "slot": -1, "type": 2, "typeName": "tile_centre",
               "targetType": "tile", "targetIndex": -1, "plane": 0, "tileX": 3222, "tileY": 3218,
               "rangeTiles": 12}
# An npc arrow carries a coordinate on the wire, as the agent's does.
_NPC_ARROW = {"source": "slot", "slot": 3, "type": 1, "typeName": "npc", "targetType": "npc",
              "targetIndex": 30, "plane": 0, "tileX": 3204, "tileY": 3205, "rangeTiles": 5}
_PLAYER_ARROW = {**_NPC_ARROW, "type": 10, "typeName": "player", "targetType": "player",
                 "targetIndex": 7}
_UNKNOWN_ARROW = {**_NPC_ARROW, "type": 99, "typeName": "unknown", "targetType": "unknown",
                  "targetIndex": -1}
_NO_COORD_ARROW = {**_TILE_ARROW, "tileX": -1, "tileY": -1}
_BAD_PLANE_ARROW = {**_TILE_ARROW, "plane": -1}


def test_hint_arrows_send_no_params_and_decode_each_field():
    game, calls = _recording({"arrows": [_TILE_ARROW, _NPC_ARROW], "truncated": False})
    got = game.hint_arrows()
    assert calls == [("query_hint_arrows", {})]
    assert got.ok and not got.unsupported and not got.truncated
    assert got.arrows == (
        HintArrow("tree", -1, 2, "tile_centre", "tile", -1, 0, 3222, 3218, 12),
        HintArrow("slot", 3, 1, "npc", "npc", 30, 0, 3204, 3205, 5),
    )


def test_hint_arrows_truncated_is_exposed():
    game, _ = _recording({"arrows": [_TILE_ARROW], "truncated": True})
    got = game.hint_arrows()
    assert got.truncated and len(got.arrows) == 1 and got.ok


def test_only_a_tile_arrow_with_a_usable_position_has_a_tile():
    tiles = [HintArrow.from_reply(r).tile for r in
             (_TILE_ARROW, _NPC_ARROW, _PLAYER_ARROW, _UNKNOWN_ARROW, _NO_COORD_ARROW,
              _BAD_PLANE_ARROW)]
    assert tiles == [Tile(3222, 3218, 0), None, None, None, None, None]


@pytest.mark.parametrize("row", [_NPC_ARROW, _PLAYER_ARROW, _UNKNOWN_ARROW, _NO_COORD_ARROW,
                                 _BAD_PLANE_ARROW])
def test_arrow_without_a_tile_never_matches_within_or_nearest(row):
    game, _ = _recording({"arrows": [row], "truncated": False})
    near = Tile(row["tileX"], row["tileY"], 0)     # standing on the coordinate it carries
    assert E.HintArrows(game).within(near, 50).count() == 0
    assert E.HintArrows(game).nearest(near) is None
    assert E.HintArrows(game).count() == 1          # still listed
    assert E.HintArrows(game).first() is not None


def test_nearest_picks_the_tile_arrow_over_a_closer_npc_arrow():
    game, _ = _recording({"arrows": [_NPC_ARROW, _TILE_ARROW], "truncated": False})
    near_the_npc = Tile(_NPC_ARROW["tileX"], _NPC_ARROW["tileY"], 0)
    assert E.HintArrows(game).nearest(near_the_npc).target_type == "tile"
    assert [a.target_type for a in E.HintArrows(game).within(Tile(3222, 3218, 0), 0).all()] == ["tile"]


def test_hint_arrow_query_chains_and_keeps_the_gate():
    game, _ = _recording({"arrows": [_NPC_ARROW, _TILE_ARROW], "truncated": False})
    query = E.HintArrows(game).where(lambda a: a.type in (1, 2))
    assert query.count() == 2
    assert query.within(Tile(3204, 3205, 0), 1).count() == 0


def test_by_type_matches_a_hint_arrow():
    game, _ = _recording({"arrows": [_NPC_ARROW, _TILE_ARROW], "truncated": False})
    assert HintArrow.from_reply(_TILE_ARROW).type_id == 2
    assert [a.type for a in E.by_type(E.HintArrows(game), 2).all()] == [2]
    assert [a.type for a in E.by_type(E.HintArrows(game), 1).all()] == [1]
    assert E.by_type(E.HintArrows(game), 10).count() == 0


@pytest.mark.parametrize("x,y", [(0, 3218), (3222, 0)])
def test_an_axis_at_zero_is_a_coordinate(x, y):
    # The agent reports only tile (0,0) as -1, so one axis at 0 is a position.
    arrow = HintArrow.from_reply({**_TILE_ARROW, "tileX": x, "tileY": y})
    assert arrow.tile == Tile(x, y, 0)


def test_hint_arrow_query_reads_a_refusal_as_no_arrows():
    game = _game(lambda m, p: "__error__:channel_unsupported")
    assert E.HintArrows(game).count() == 0
    assert E.HintArrows(game).nearest(Tile(1, 1, 0)) is None


def test_entity_query_with_a_position_for_every_row_is_unchanged():
    rows = [Tile(10, 10, 0), Tile(2, 2, 0), Tile(100, 100, 0)]
    query = E.EntityQuery(rows, lambda t: t)
    assert query.nearest(Tile(0, 0, 0)) == Tile(2, 2, 0)
    assert query.within(Tile(0, 0, 0), 10).all() == [Tile(10, 10, 0), Tile(2, 2, 0)]


def test_overhead_text_sends_the_target_and_decodes():
    game, calls = _recording({"text": "Cheep!", "expiresInMs": 1800, "truncated": False})
    got = game.overhead_text("npc", 30)
    assert calls == [("get_overhead_text", {"targetType": "npc", "serverIndex": 30})]
    assert got == OverheadText("Cheep!", 1800, False)
    assert got.ok


def test_overhead_text_truncated_and_player_target():
    game, calls = _recording({"text": "Café", "expiresInMs": 0, "truncated": True})
    got = game.overhead_text("player", 7)
    assert calls[0][1] == {"targetType": "player", "serverIndex": 7}
    assert got.text == "Café" and got.truncated


def test_silent_overhead_text_is_not_an_error():
    game, _ = _recording({"text": None})
    got = game.overhead_text("npc", 31)
    assert got.ok and got.text is None
    assert (got.expires_in_ms, got.truncated) == (0, False)


def test_headbars_send_the_target_and_decode_each_field():
    reply = {"headbars": [
        {"headbarId": 0, "value": 128, "valueStart": 200, "startCycle": 5000, "duration": 30},
        {"headbarId": -1, "value": 0, "valueStart": 0, "startCycle": 0, "duration": 0},
    ], "truncated": False}
    game, calls = _recording(reply)
    got = game.headbars("npc", 30)
    assert calls == [("get_headbars", {"targetType": "npc", "serverIndex": 30})]
    assert got.ok and not got.truncated
    assert got.headbars == (Headbar(0, 128, 200, 5000, 30), Headbar(-1, 0, 0, 0, 0))


def test_headbars_truncated_is_exposed_and_an_empty_list_is_ok():
    game, _ = _recording({"headbars": [], "truncated": True})
    assert game.headbars("player", 7).truncated
    game, _ = _recording({"headbars": [], "truncated": False})
    got = game.headbars("npc", 31)
    assert got.ok and got.headbars == () and not got.truncated


def test_headbar_has_no_value_end():
    # The agent dropped it: `value` is the end of the interpolation. A reply must not need one.
    assert not hasattr(Headbar(0, 1, 2, 3, 4), "value_end")


_ALL_REFUSALS = ["not_in_world", "not_found", "channel_unsupported", "game_thread_timeout"]


@pytest.mark.parametrize("code", _ALL_REFUSALS)
def test_a_refusal_is_a_typed_value_on_every_read(code):
    game = _game(lambda m, p: f"__error__:{code}")
    want = EntityReadStatus(code)
    arrows, text, bars = game.hint_arrows(), game.overhead_text("npc", 1), game.headbars("npc", 1)
    assert (arrows.status, text.status, bars.status) == (want, want, want)
    assert not arrows.ok and not text.ok and not bars.ok
    assert arrows.arrows == () and text.text is None and bars.headbars == ()
    assert not arrows.truncated and not text.truncated and not bars.truncated


def test_only_channel_unsupported_reads_as_unsupported():
    assert _game(lambda m, p: "__error__:channel_unsupported").headbars("npc", 1).unsupported
    assert _game(lambda m, p: "__error__:channel_unsupported").hint_arrows().unsupported
    assert _game(lambda m, p: "__error__:channel_unsupported").overhead_text("npc", 1).unsupported
    assert not _game(lambda m, p: "__error__:not_found").headbars("npc", 1).unsupported
    assert not HeadbarList().unsupported and not HintArrowList().unsupported


def test_refused_differs_from_silent():
    silent = _game(lambda m, p: {"text": None}).overhead_text("npc", 1)
    refused = _game(lambda m, p: "__error__:not_found").overhead_text("npc", 1)
    assert silent.text is None and refused.text is None
    assert silent.ok and not refused.ok


@pytest.mark.parametrize("message", ["rpc timeout", "get_headbars: bad params", "not_found "])
def test_anything_else_still_raises(message):
    game = _game(lambda m, p: f"__error__:{message}")
    with pytest.raises(RpcError) as ei:
        game.headbars("npc", 1)
    assert ei.value.agent_message == message


def test_a_dead_transport_still_raises():
    game = _game(lambda m, p: "__error__:pipe disconnected")
    with pytest.raises(ConnectionError):
        game.hint_arrows()


@pytest.mark.parametrize("kind", ["loc", "NPC", "", None])
def test_a_bad_kind_is_refused_before_any_call(kind):
    game, calls = _recording({"headbars": [], "truncated": False})
    with pytest.raises(ValueError):
        game.headbars(kind, 1)
    with pytest.raises(ValueError):
        game.overhead_text(kind, 1)
    assert calls == []


_BAR = {"headbarId": 0, "value": 1, "valueStart": 2, "startCycle": 3, "duration": 4}


@pytest.mark.parametrize("decode,reply", [
    (HintArrowList.from_reply, {"arrows": []}),
    (HintArrowList.from_reply, {"truncated": False}),
    (HintArrowList.from_reply, {"arrows": [], "truncated": None}),
    (HintArrowList.from_reply, {"arrows": {}, "truncated": False}),
    (HeadbarList.from_reply, {"headbars": []}),
    (HeadbarList.from_reply, {"headbars": [], "truncated": None}),
    (HeadbarList.from_reply, {"headbars": [], "truncated": "false"}),
    (HeadbarList.from_reply, {"headbars": {}, "truncated": False}),
    (OverheadText.from_reply, {}),
    (OverheadText.from_reply, {"text": "x", "truncated": False}),
    (OverheadText.from_reply, {"text": "x", "expiresInMs": 1}),
    (OverheadText.from_reply, {"text": "x", "expiresInMs": 1, "truncated": None}),
    (OverheadText.from_reply, {"text": "x", "expiresInMs": 1, "truncated": "false"}),
    (OverheadText.from_reply, {"text": 123, "expiresInMs": 1, "truncated": False}),
    (OverheadText.from_reply, {"text": "x", "expiresInMs": None, "truncated": False}),
    (HintArrow.from_reply, {**_TILE_ARROW, "tileY": None}),
    (HintArrow.from_reply, {**_TILE_ARROW, "tileX": True}),
    (HintArrow.from_reply, {**_TILE_ARROW, "source": None}),
    (HintArrow.from_reply, {**_TILE_ARROW, "typeName": 2}),
    (HintArrow.from_reply, {**_TILE_ARROW, "targetType": None}),
    (Headbar.from_reply, {**_BAR, "value": "1"}),
    (Headbar.from_reply, {**_BAR, "duration": None}),
])
def test_a_malformed_reply_raises_rather_than_inventing_a_default(decode, reply):
    with pytest.raises((KeyError, TypeError)):
        decode(reply)


_READS = [lambda g: g.hint_arrows(), lambda g: g.overhead_text("npc", 1),
          lambda g: g.headbars("npc", 1)]


@pytest.mark.parametrize("read", _READS)
@pytest.mark.parametrize("reply", [None, [], 5, "ok", {}, {"truncated": None, "text": 1}])
def test_a_bad_reply_is_refused_as_malformed_never_ok_and_empty(read, reply):
    game = _game(lambda m, p: reply)
    with pytest.raises(MalformedReply) as ei:
        read(game)
    assert isinstance(ei.value, RpcError)      # one except covers every failure to answer
    assert "malformed reply" in ei.value.agent_message


def test_the_query_form_does_not_hide_a_malformed_reply():
    with pytest.raises(MalformedReply):
        E.HintArrows(_game(lambda m, p: None))


@pytest.mark.parametrize("read", _READS)
def test_a_refused_result_is_truthy_as_documented(read):
    assert read(_game(lambda m, p: "__error__:channel_unsupported"))


@pytest.mark.parametrize("method", [GameApi.hint_arrows, GameApi.overhead_text, GameApi.headbars])
def test_each_read_documents_how_a_refusal_looks(method):
    for term in (".ok", ".unsupported", "truthy", "CHANNEL_UNSUPPORTED", "NOT_IN_WORLD",
                 "GAME_THREAD_TIMEOUT"):
        assert term in method.__doc__, term
