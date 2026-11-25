"""Pin the wire-contract literals. Mirrors the Java host's LayoutWireOffsetsTest:

if the agent's SharedLayout.h moves an offset, this fails loudly rather than the client
silently reading garbage.

The authority is ``tests/fixtures/shared_layout.json``: tools/layout_dump/dump_layout.cpp
compiled against the producer's own SharedLayout.h, so its numbers are offsetof/sizeof
results, not literals typed here. The fixture records the producer commit and the header's
git blob id; regenerate it (tools/layout_dump/README.md) on every protocol bump.
"""
import json
import struct
from pathlib import Path

from botwithus.core import layout as L
from botwithus.core.snapshot import SnapshotView
from fakes import build_snapshot

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "shared_layout.json").read_text())
_PRODUCER = _FIXTURE["layout"]

# Every layout.py constant family the dumper emits. A constant in one of these families that
# the producer header does not know fails too, so a field cannot be added on one side only.
_FAMILIES = ("SNAP_", "SZ_", "LP_", "NPC_", "PL_")


def test_fixture_is_the_v25_producer_header():
    assert _FIXTURE["protocol_version"] == 25
    assert _FIXTURE["producer_sha"] == "412510a5bd307e3249941cb75e6f728ca5ebde86"
    assert _FIXTURE["header_blob"] == "210cd099baf3789e46be27f1f4b5c698346f7033"


def test_version_and_magic():
    assert L.PROTOCOL_VERSION == _FIXTURE["protocol_version"]
    assert L.SNAPSHOT_MAGIC == 0x5354584E
    assert L.SNAPSHOT_MAGIC_BYTES == b"NXTS"
    assert L.SNAPSHOT_SIZE == _PRODUCER["SNAPSHOT_SIZE"]
    assert L.HEADER_SIZE == 64


def test_every_emitted_offset_matches_the_producer_header():
    mismatches = {name: (getattr(L, name, None), want) for name, want in _PRODUCER.items()
                  if getattr(L, name, None) != want}
    assert mismatches == {}


def test_no_layout_constant_is_unknown_to_the_producer_header():
    declared = {name for name in dir(L) if name.startswith(_FAMILIES)}
    assert declared - set(_PRODUCER) == set()


def test_name_flag_and_capacity_match_the_producer_header():
    assert L.PLAYER_NAME_CAP == _PRODUCER["PLAYER_NAME_CAP"]
    assert L.FLAG_MOVING == _PRODUCER["FLAG_MOVING"]
    assert L.FLAG_NAME_TRUNCATED == _PRODUCER["FLAG_NAME_TRUNCATED"]


def _v25_snapshot_chain() -> dict[str, int]:
    """Every Snapshot field offset, rebuilt from row sizes and caps alone.

    The same sum SharedLayout.h's static_asserts use, with the sizes and caps written as
    literals rather than read from ``layout``, so a wrong constant in layout.py cannot
    agree with itself here. An independent second derivation beside the fixture.
    """
    sizes = [  # (layout name, bytes the field occupies)
        ("SNAP_PUBLISH_SEQ", 8), ("SNAP_GAME_STATE", 4), ("SNAP_OWN_INDEX", 4),
        ("SNAP_ROOT_IFACE_ID", 4), ("SNAP_SERVER_TICK", 4), ("SNAP_SELF", 568),
        ("SNAP_NPC_COUNT", 4), ("SNAP_NPCS", 48 * 1024),
        ("SNAP_PLAYER_COUNT", 4), ("SNAP_PLAYERS", 56 * 2048),
        ("SNAP_LOCATION_COUNT", 4), ("SNAP_LOCATIONS", 24 * 8192), (None, 4),  # pad
        ("SNAP_INVENTORY_COUNT", 4), ("SNAP_INVENTORIES", 8 * 32),
        ("SNAP_INV_ITEM_COUNT", 4), ("SNAP_INV_ITEMS", 8 * 2048),
        ("SNAP_PRODUCER", 32),
        ("SNAP_OPEN_IFACE_COUNT", 4), ("SNAP_OPEN_IFACE_TOTAL", 4),
        ("SNAP_OPEN_IFACES", 4 * 256), ("SNAP_OPEN_IFACE_FLAGS", 1 * 256),
        ("SNAP_OPEN_IFACE_PAD", 4),
        ("SNAP_GROUND_ITEM_COUNT", 4), ("SNAP_GROUND_ITEMS", 16 * 1024),
        ("SNAP_PROJECTILE_COUNT", 4), ("SNAP_PROJECTILES", 32 * 256),
        ("SNAP_GAME_CYCLE", 4), ("SNAP_DYN_REGION", 36),
        ("SNAP_DYN_CHUNK_COUNT", 4), ("SNAP_DYN_CHUNKS", 4 * 16384),
    ]
    chain, offset = {}, 0
    for name, size in sizes:
        if name is not None:
            chain[name] = offset
        offset += size
    chain["SNAPSHOT_SIZE"] = offset
    return chain


def test_every_snapshot_offset_matches_the_v25_chain():
    # dir() rather than a hand list: a SNAP_* constant this chain does not know about fails
    # too, so a field added to layout.py cannot skip this check.
    chain = _v25_snapshot_chain()
    declared = {name: getattr(L, name) for name in dir(L) if name.startswith("SNAP_")}
    declared["SNAPSHOT_SIZE"] = L.SNAPSHOT_SIZE
    assert declared == chain


# The v24 offsets, as protocol v24 published them. v25 grew self by 8 and each PlayerEntry by 8:
# npcs[] and players[] moved +8, and everything from locationCount on +8 + 8 * 2048.
_V24_OFFSETS = {
    "SNAP_SELF": 24, "SNAP_NPC_COUNT": 584, "SNAP_NPCS": 588, "SNAP_PLAYER_COUNT": 49_740,
    "SNAP_PLAYERS": 49_744, "SNAP_LOCATION_COUNT": 148_048, "SNAP_PRODUCER": 361_312,
    "SNAP_OPEN_IFACE_FLAGS": 362_376, "SNAP_GROUND_ITEM_COUNT": 362_636,
    "SNAP_GAME_CYCLE": 387_220, "SNAP_DYN_REGION": 387_224, "SNAP_DYN_CHUNKS": 387_264,
    "SNAPSHOT_SIZE": 452_800,
}
_V25_SHIFT = {"SNAP_SELF": 0, "SNAP_NPC_COUNT": 8, "SNAP_NPCS": 8,
              "SNAP_PLAYER_COUNT": 8, "SNAP_PLAYERS": 8}
_V25_TAIL_SHIFT = 16_392


def test_v25_moved_each_field_by_the_rows_ahead_of_it():
    for name, v24 in _V24_OFFSETS.items():
        assert getattr(L, name) == v24 + _V25_SHIFT.get(name, _V25_TAIL_SHIFT), name


def test_open_iface_block_v23():
    # v23: openIfaceFlags (u8[256]) between the id array and the pad, index-parallel to ids.
    assert L.CAP_OPEN_IFACES == 256
    assert L.SNAP_OPEN_IFACES + L.CAP_OPEN_IFACES * 4 == L.SNAP_OPEN_IFACE_FLAGS
    assert L.SNAP_OPEN_IFACE_FLAGS + L.CAP_OPEN_IFACES == L.SNAP_OPEN_IFACE_PAD
    assert L.SNAP_OPEN_IFACE_PAD + 4 == L.SNAP_GROUND_ITEM_COUNT


def test_open_iface_flag_bits_v23():
    assert L.OPEN_IFACE_TYPE_MASK == 0x07
    assert L.OPEN_IFACE_FLAG_CLIENT_OPENED == 0x08
    assert (L.OPEN_IFACE_TYPE_MODAL, L.OPEN_IFACE_TYPE_OVERLAY,
            L.OPEN_IFACE_TYPE_CHILD, L.OPEN_IFACE_TYPE_UNKNOWN) == (0, 1, 3, 7)


def _with_open_ifaces(count: int, total: int, ids: dict[int, int],
                      flags: dict[int, int] | None = None) -> SnapshotView:
    # Literal offsets, not layout constants, for the same reason as the location test below.
    buf = bytearray(build_snapshot())
    struct.pack_into("<II", buf, 377_736, count, total)     # openIfaceCount, openIfaceTotal
    for index, iface_id in ids.items():
        struct.pack_into("<i", buf, 377_744 + index * 4, iface_id)
    for index, flag in (flags or {}).items():
        buf[378_768 + index] = flag                         # openIfaceFlags[index]
    return SnapshotView(bytes(buf))


def test_open_iface_flags_decode_type_cs2_bit_and_modality():
    # Five rows inside the count and one past it. Rows 3 and 4 set the reserved bits 4-7,
    # which must be masked out rather than read as a type.
    ids = {0: 517, 1: 1477, 2: 1432, 3: 1188, 4: 1500, 5: 999}
    flags = {0: 0x00, 1: 0x01, 2: 0x0B, 3: 0xF0, 4: 0xF7, 5: 0x00}
    snap = _with_open_ifaces(5, 6, ids, flags)

    got = [(e.iface_id, e.type, e.client_opened, e.is_modal) for e in snap.open_iface_entries()]
    assert got == [
        (517, 0, False, True),     # bank: modal
        (1477, 1, False, False),   # HUD panel: overlay
        (1432, 3, True, False),    # CS2-opened child
        (1188, 0, False, True),    # modal under reserved bits
        (1500, 7, False, False),   # unknown type under reserved bits
    ]
    assert snap.open_ifaces() == [517, 1477, 1432, 1188, 1500]
    assert snap.modal_ifaces() == [517, 1188]
    assert snap.is_modal_open()


def test_no_modal_open_when_only_overlays_are():
    snap = _with_open_ifaces(2, 2, {0: 1477, 1: 1432}, {0: 0x01, 1: 0x0B})
    assert snap.modal_ifaces() == []
    assert not snap.is_modal_open()


def test_ground_items_decode_at_the_v25_offset():
    # Past every grown row. Literal offsets: 379028 count, 379032 rows.
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, 379_028, 1)
    struct.pack_into("<iihhb", buf, 379_032, 1511, 3, 3210, 3211, 0)
    (item,) = SnapshotView(bytes(buf)).ground_items()
    assert (item.item_id, item.quantity, item.tile_x, item.tile_y) == (1511, 3, 3210, 3211)


def test_game_cycle_and_dyn_region_decode_at_the_v25_offsets():
    # The tail of the moved block: gameCycle 403612, dynRegion 403616.
    buf = bytearray(build_snapshot())
    struct.pack_into("<i", buf, 403_612, 777_001)
    buf[403_616] = 1                                      # dynRegion.isInstance
    snap = SnapshotView(bytes(buf))
    assert snap.game_cycle == 777_001
    assert snap.dynamic_region.is_instance


def test_open_iface_in_last_slot_of_full_list_is_found():
    # The bank (517) in slot 255: the v21 cap of 64 never read past slot 63.
    snap = _with_open_ifaces(256, 256, {255: 517})
    assert len(snap.open_ifaces()) == 256
    assert snap.is_interface_open(517)
    assert snap.open_iface_total() == 256
    assert snap.is_open_iface_list_complete()


def test_open_iface_list_incomplete_when_total_exceeds_count():
    snap = _with_open_ifaces(256, 300, {})
    assert snap.open_iface_total() == 300
    assert not snap.is_open_iface_list_complete()


def test_open_iface_list_never_complete_when_table_unreadable():
    snap = _with_open_ifaces(0, 0, {})
    assert snap.open_ifaces() == []
    assert not snap.is_open_iface_list_complete()


def test_location_entry_v20():
    # v20 appended resolvedId, growing the row 20 -> 24; the earlier fields did not move.
    assert L.SZ_LOCATION_ENTRY == 24
    assert L.LOC_SHAPE == 17
    assert L.LOC_ROTATION == 18
    assert L.LOC_FLAGS == 19
    assert L.LOC_RESOLVED_ID == 20


def test_entity_rows_v25():
    # v21 appended orientation; v24 appended runEnergy/runWeight to LocalPlayer,
    # activeTypeId/combatLevel to NpcEntry and name[16] to PlayerEntry; v25 appended the server
    # tile to LocalPlayer and PlayerEntry. Earlier fields stay.
    assert (L.SZ_NPC_ENTRY, L.NPC_ORIENTATION, L.NPC_ACTIVE_TYPE_ID, L.NPC_COMBAT_LEVEL)         == (48, 36, 40, 44)
    assert (L.SZ_PLAYER_ENTRY, L.PL_ORIENTATION, L.PL_NAME, L.PLAYER_NAME_CAP) == (56, 28, 32, 16)
    assert (L.SZ_LOCAL_PLAYER, L.LP_ORIENTATION, L.LP_SKILL_COUNT) == (568, 32, 36)
    assert (L.LP_RUN_ENERGY, L.LP_RUN_WEIGHT) == (552, 556)
    assert (L.LP_SERVER_TILE_X, L.LP_SERVER_TILE_Y, L.LP_SERVER_PLANE) == (560, 562, 564)
    assert (L.PL_SERVER_TILE_X, L.PL_SERVER_TILE_Y, L.PL_SERVER_PLANE) == (48, 50, 52)


def test_substruct_sizes_tile_array_math():
    # Each array's declared start + cap*size must not overrun the next field.
    assert L.SNAP_NPCS + L.CAP_NPCS * L.SZ_NPC_ENTRY <= L.SNAP_PLAYER_COUNT
    assert L.SNAP_PLAYERS + L.CAP_PLAYERS * L.SZ_PLAYER_ENTRY <= L.SNAP_LOCATION_COUNT
    assert L.SNAP_LOCATIONS + L.CAP_LOCATIONS * L.SZ_LOCATION_ENTRY <= L.SNAP_INVENTORY_COUNT
    assert L.SNAP_INV_ITEMS + L.CAP_INV_ITEMS * L.SZ_INVENTORY_ITEM <= L.SNAP_PRODUCER
    assert L.SNAP_DYN_CHUNKS + L.CAP_DYN_CHUNKS * 4 == L.SNAPSHOT_SIZE


def test_event_ring_geometry():
    assert L.RING_SLOT_COUNT_EXPECTED == 1024
    assert L.RING_SLOT_MASK_EXPECTED == 1023
    assert L.EVENT_SLOT_SIZE == 128
    assert L.ES_BODY == 16
    assert L.EVENT_BODY_MAX == 112


def test_second_location_row_decodes_at_the_v21_stride():
    # Literal byte offsets, not layout constants: a stride or field regression in layout.py
    # must not be able to move the writer and the reader together and still pass.
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, 164_440, 2)         # locationCount
    row1 = 164_444 + 24                             # locations[1]
    struct.pack_into("<iii", buf, row1, 125_195, 125_195, -1)
    struct.pack_into("<hhbBBB", buf, row1 + 12, 3200, 3201, 0, 10, 3, 0)
    struct.pack_into("<i", buf, row1 + 20, 125_205)

    loc = SnapshotView(bytes(buf)).locations()[1]
    assert (loc.interact_id, loc.tile_x, loc.tile_y) == (125_195, 3200, 3201)
    assert (loc.shape, loc.rotation) == (10, 3)
    assert loc.resolved_id == 125_205
