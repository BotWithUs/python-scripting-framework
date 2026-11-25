"""Wire v21 facing, decoded end to end through SnapshotView.

The producer's bytes go in at LITERAL offsets from NXTLibrary's SharedLayout.h (v25 strides), not
through `layout`: a test that wrote through the reader's own constants would pass whatever
they said. Row 1 is written as well as row 0, so the stride is pinned with the field.
"""
import struct

from botwithus.api import orientation as O
from botwithus.api.orientation import Direction, WireOrientationDecoder
from botwithus.core.snapshot import SnapshotView
from fakes import build_snapshot

NPC_COUNT, NPCS, NPC_STRIDE, NPC_ORIENTATION = 592, 596, 48, 36
PLAYER_COUNT, PLAYERS, PLAYER_STRIDE, PLAYER_ORIENTATION = 49_748, 49_752, 56, 28
SELF, SELF_ORIENTATION = 24, 32
OUT_OF_CONTRACT = 0x4000


def _u16(buf, off, value):
    struct.pack_into("<H", buf, off, value)


def test_npc_rows_decode_facing_and_the_sentinel_as_unknown():
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, NPC_COUNT, 2)
    _u16(buf, NPCS + NPC_ORIENTATION, O.WIRE_UNKNOWN)
    _u16(buf, NPCS + NPC_STRIDE + NPC_ORIENTATION, 12288)

    npcs = SnapshotView(bytes(buf)).npcs()
    assert npcs[0].orientation == O.UNKNOWN_RAW
    assert npcs[0].facing is None
    assert npcs[1].orientation == 12288
    assert npcs[1].facing is Direction.EAST


def test_player_rows_decode_facing_with_read_back_tolerance():
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, PLAYER_COUNT, 2)
    _u16(buf, PLAYERS + PLAYER_ORIENTATION, 4096)
    _u16(buf, PLAYERS + PLAYER_STRIDE + PLAYER_ORIENTATION, O.NORTH_RAW - 1)

    players = SnapshotView(bytes(buf)).players()
    assert players[0].facing is Direction.WEST
    assert players[1].orientation == O.NORTH_RAW - 1
    assert O.is_same_facing(players[1].orientation, O.NORTH_RAW)


def test_self_decodes_facing_from_the_reused_pad():
    buf = bytearray(build_snapshot())
    _u16(buf, SELF + SELF_ORIENTATION, 4096)

    me = SnapshotView(bytes(buf)).self_player
    assert me.orientation == 4096
    assert me.facing_degrees == 270.0


def test_out_of_contract_facing_degrades_and_is_reported_once_per_decoder():
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, NPC_COUNT, 2)
    _u16(buf, NPCS + NPC_ORIENTATION, OUT_OF_CONTRACT)
    _u16(buf, NPCS + NPC_STRIDE + NPC_ORIENTATION, OUT_OF_CONTRACT)
    reported = []
    session = WireOrientationDecoder(reported.append)

    first = SnapshotView(bytes(buf), session).npcs()
    second = SnapshotView(bytes(buf), session).npcs()

    assert [n.orientation for n in first + second] == [O.UNKNOWN_RAW] * 4
    assert reported == [OUT_OF_CONTRACT]
