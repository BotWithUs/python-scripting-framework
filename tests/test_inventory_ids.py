"""Pins the RS3 inventory ids: 93 = backpack, 94 = worn equipment, 95 = bank.

Until this test, ``layout`` had bank and equipment swapped (94 / 95), so ``bank()``
returned the worn items and ``equipment()`` returned the bank, or nothing while the
bank was closed. The correct ids come from three sources that agree:

- the gameval table (etype ``inv``): 93 INV, 94 WORN, 95 BANK;
- the Java host's ``InventoryIds``: EQUIPMENT = 94, BANK = 95;
- a live read through a v21 agent on 2026-10-06: inventory 94 had 19 slots holding
  worn gear (a staff, gloves, boots, a drape) with the bank open or closed, and
  inventory 95 had 1920 slots of bank stacks, published only while the bank
  interface was open.

The snapshot fixtures below mirror that live shape.
"""
import struct

from botwithus.api import inventory
from botwithus.core import layout as L
from botwithus.core.snapshot import SnapshotView

from fakes import build_snapshot

WORN_SLOTS = 19
BANK_SLOTS = 1920
BACKPACK_SLOTS = 28

STAFF = 1381
GLOVES = 1059
BANKED_LOGS = 1511
BANKED_LOGS_QTY = 5000
BACKPACK_KNIFE = 946


def _with_inventories(buf: bytes, inventories: list[tuple[int, int, dict[int, tuple[int, int]]]]) -> bytes:
    """Write ``(inv_id, slot_count, {slot: (item_id, qty)})`` headers + items into ``buf``."""
    out = bytearray(buf)
    struct.pack_into("<I", out, L.SNAP_INVENTORY_COUNT, len(inventories))
    first = 0
    for index, (inv_id, slot_count, filled) in enumerate(inventories):
        header = L.SNAP_INVENTORIES + index * L.SZ_INVENTORY_HEADER
        struct.pack_into("<i", out, header + L.INVH_INV_ID, inv_id)
        struct.pack_into("<H", out, header + L.INVH_SLOT_COUNT, slot_count)
        struct.pack_into("<H", out, header + L.INVH_FIRST_ITEM_IDX, first)
        for slot in range(slot_count):
            item_id, quantity = filled.get(slot, (-1, 0))
            item = L.SNAP_INV_ITEMS + (first + slot) * L.SZ_INVENTORY_ITEM
            struct.pack_into("<i", out, item + L.INVI_ITEM_ID, item_id)
            struct.pack_into("<i", out, item + L.INVI_QUANTITY, quantity)
        first += slot_count
    struct.pack_into("<I", out, L.SNAP_INV_ITEM_COUNT, first)
    return bytes(out)


BACKPACK = (93, BACKPACK_SLOTS, {0: (BACKPACK_KNIFE, 1)})
WORN = (94, WORN_SLOTS, {3: (STAFF, 1), 9: (GLOVES, 1)})
BANK = (95, BANK_SLOTS, {0: (BANKED_LOGS, BANKED_LOGS_QTY)})


def _snapshot(*inventories) -> SnapshotView:
    return SnapshotView(_with_inventories(build_snapshot(), list(inventories)))


def test_inventory_ids_match_gameval():
    assert (L.INV_BACKPACK, L.INV_EQUIPMENT, L.INV_BANK) == (93, 94, 95)


def test_equipment_reads_the_worn_inventory_with_the_bank_open():
    snapshot = _snapshot(BACKPACK, WORN, BANK)
    worn = inventory.equipment(snapshot)
    assert len(worn.items) == WORN_SLOTS
    assert worn.contains(STAFF) and worn.contains(GLOVES)
    assert not worn.contains(BANKED_LOGS)


def test_bank_reads_the_bank_inventory_with_the_bank_open():
    snapshot = _snapshot(BACKPACK, WORN, BANK)
    bank = inventory.bank(snapshot)
    assert len(bank.items) == BANK_SLOTS
    assert bank.count(BANKED_LOGS) == BANKED_LOGS_QTY
    assert not bank.contains(STAFF)


def test_bank_closed_leaves_equipment_intact_and_bank_empty():
    snapshot = _snapshot(BACKPACK, WORN)
    assert inventory.equipment(snapshot).contains(STAFF)
    assert inventory.bank(snapshot).items == ()
    assert inventory.bank(snapshot).is_empty()


def test_backpack_is_unaffected():
    snapshot = _snapshot(BACKPACK, WORN, BANK)
    assert inventory.backpack(snapshot).occupied()[0].item_id == BACKPACK_KNIFE
