"""Cache definitions (botwithus.api.definitions) and the inventory's default ``names=`` resolver,
over a fake ``_native`` in the real binding's shape (native ABI 7: cache_item / cache_npc /
cache_loc / cache_params / cache_json / cache_status).

Failure means: a field mapped from the wrong key, None where the binding has a definition, a
host error swallowed instead of raised as CacheUnavailable, the default resolver not used when
names= is omitted, or a noted item not matching its unnoted name.
"""
import json
import struct

import pytest

from botwithus.api import definitions as D
from botwithus.api.game import GameApi
from botwithus.core import layout as L
from botwithus.core import native as native_mod
from botwithus.core.native import NativeUnavailable
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot

ITEMS = {
    1511: {"id": 1511, "name": "Logs", "members": False, "stackable": False, "noted": False,
           "noted_id": 1512, "category": 22, "shop_price": 4, "ge_buy_limit": 25000,
           "exchangeable": True, "wearpos": -1, "wearpos2": -1, "wearpos3": -1, "param_count": 5,
           "text_truncated": 0, "inventory_ops": ["Craft", "Light", "", "", ""],
           "ground_ops": ["", "", "Take", "Light", ""]},
    1512: {"id": 1512, "name": "Logs", "members": False, "stackable": True, "noted": True,
           "noted_id": 1511, "category": 0, "shop_price": 4, "ge_buy_limit": 0,
           "exchangeable": True, "wearpos": -1, "wearpos2": -1, "wearpos3": -1, "param_count": 0,
           "text_truncated": 0, "inventory_ops": ["", "", "", "", ""],
           "ground_ops": ["", "", "Take", "", ""]},
    4151: {"id": 4151, "name": "Abyssal whip", "members": True, "stackable": False, "noted": False,
           "noted_id": 4152, "category": 150, "shop_price": 120001, "ge_buy_limit": 70,
           "exchangeable": True, "wearpos": 3, "wearpos2": 5, "wearpos3": -1, "param_count": 2,
           "text_truncated": 0, "inventory_ops": ["", "Wield", "", "", ""],
           "ground_ops": ["", "", "Take", "", ""]},
}


class CacheNative(FakeNative):
    """A bwu_host with the ABI 7 cache reads. ``fail`` plays a host that cannot open the cache."""

    fail: str | None = None

    def __init__(self, **kw):
        super().__init__(**kw)
        self.item_calls: list[int] = []

    def _check(self):
        if self.fail:
            raise RuntimeError(self.fail)

    def cache_status(self):
        self._check()
        return 1

    def cache_item(self, item_id):
        self._check()
        self.item_calls.append(item_id)
        if item_id < 0:
            raise ValueError("a cache id is never negative")
        return ITEMS.get(item_id)

    def cache_npc(self, npc_id):
        self._check()
        return {"id": 2001, "name": "Fixture guard", "combat_level": 88, "visible": True,
                "clickable": False, "varbit_id": 555, "varp_id": -1, "transform_count": 3,
                "param_count": 1, "text_truncated": 0,
                "ops": ["Talk-to", "", "Attack", "", ""]} if npc_id == 2001 else None

    def cache_loc(self, loc_id):
        self._check()
        return {"id": 3001, "name": "Fixture door", "size_x": 2, "size_y": 3, "interact_type": 2,
                "solid_type": 1, "members": True, "varbit_id": -1, "varp_id": 1234,
                "transform_count": 1, "map_sprite_id": 17, "param_count": 0, "text_truncated": 0,
                "ops": ["Open", "", "", "", "Examine"]} if loc_id == 3001 else None

    def cache_params(self, cache_type, def_id):
        self._check()
        if (cache_type, def_id) != (D.CACHE_ITEM, 4151):
            return None
        return [{"param_id": 14, "kind": 0, "value": 4, "text": None, "text_truncated": False},
                {"param_id": 555, "kind": 1, "value": None, "text": "Whip", "text_truncated": True}]

    def cache_json(self, cache_type, def_id):
        self._check()
        if (cache_type, def_id) == (D.CACHE_ENUM, 5001):
            return json.dumps({"id": 5001, "entries": {"1": "one"}})
        if (cache_type, def_id) == (D.CACHE_QUEST, 1):
            return json.dumps({"id": 1, "listName": "Tower of Life", "membersOnly": True})
        return None


@pytest.fixture(autouse=True)
def _fresh_names():
    D.clear_item_names()
    yield
    D.clear_item_names()


def test_constants_match_the_host_header():
    assert (D.CACHE_ITEM, D.CACHE_NPC, D.CACHE_LOC, D.CACHE_ENUM, D.CACHE_STRUCT, D.CACHE_PARAM,
            D.CACHE_VARBIT, D.CACHE_INV, D.CACHE_QUEST, D.CACHE_WORLDMAP) == tuple(range(10))
    assert (D.PARAM_INT, D.PARAM_STRING) == (0, 1)


def test_item_npc_loc_are_typed():
    native_mod.set_backend(CacheNative())
    defs = D.Definitions()
    whip = defs.item(4151)
    assert (whip.name, whip.members, whip.noted_id, whip.category, whip.wearpos2) == \
        ("Abyssal whip", True, 4152, 150, 5)
    assert whip.inventory_ops == ("", "Wield", "", "", "") and whip.has_inventory_op("wield")
    note = defs.item(1512)
    assert note.noted and note.stackable and note.name == "Logs" and note.noted_id == 1511
    npc = defs.npc(2001)
    assert (npc.name, npc.combat_level, npc.clickable, npc.varbit_id, npc.ops[2]) == \
        ("Fixture guard", 88, False, 555, "Attack")
    loc = defs.loc(3001)
    assert (loc.size_x, loc.size_y, loc.varp_id, loc.map_sprite_id, loc.ops[4]) == (2, 3, 1234, 17, "Examine")
    assert defs.item(999) is None and defs.npc(1) is None and defs.loc(1) is None


def test_params_and_the_json_tail():
    native_mod.set_backend(CacheNative())
    defs = D.Definitions()
    params = defs.params(D.CACHE_ITEM, 4151)
    assert [(p.param_id, p.kind, p.value, p.text, p.text_truncated) for p in params] == \
        [(14, D.PARAM_INT, 4, None, False), (555, D.PARAM_STRING, None, "Whip", True)]
    assert defs.int_param(D.CACHE_ITEM, 4151, 14) == 4
    assert defs.int_param(D.CACHE_ITEM, 4151, 555) is None   # a string param is not an int
    assert defs.params(D.CACHE_ITEM, 1) is None
    assert defs.enum(5001) == {"id": 5001, "entries": {"1": "one"}}
    assert defs.quest(1)["listName"] == "Tower of Life"
    assert defs.struct(1) is None


def test_errors_are_raised_not_swallowed():
    be = CacheNative()
    be.fail = "NXTCache.dll not found in ~/.botwithus/native"
    native_mod.set_backend(be)
    defs = D.Definitions()
    with pytest.raises(D.CacheUnavailable, match="NXTCache.dll not found"):
        defs.item(4151)
    assert defs.available() is False
    native_mod.set_backend(CacheNative())
    with pytest.raises(ValueError, match="never negative"):
        defs.item(-1)


def test_an_older_host_says_it_predates_the_cache():
    native_mod.set_backend(FakeNative())
    with pytest.raises(NativeUnavailable, match="ABI 7"):
        D.Definitions().item(4151)
    assert D.Definitions().available() is False


def test_item_names_are_kept_on_this_side():
    be = CacheNative()
    native_mod.set_backend(be)
    assert D.item_name(4151) == "Abyssal whip"
    assert D.item_name(4151) == "Abyssal whip"
    assert D.item_name(-1) is None
    assert be.item_calls == [4151]   # once; an empty slot (-1) never asks


def _backpack(game_items):
    """A backpack (inventory 93, 28 slots) holding ``{slot: (item_id, qty)}``."""
    out = bytearray(build_snapshot())
    struct.pack_into("<I", out, L.SNAP_INVENTORY_COUNT, 1)
    struct.pack_into("<iHH", out, L.SNAP_INVENTORIES, 93, 28, 0)
    for slot in range(28):
        item_id, quantity = game_items.get(slot, (-1, 0))
        struct.pack_into("<ii", out, L.SNAP_INV_ITEMS + slot * L.SZ_INVENTORY_ITEM, item_id, quantity)
    struct.pack_into("<I", out, L.SNAP_INV_ITEM_COUNT, 28)
    be = CacheNative(snapshot=bytes(out))
    native_mod.set_backend(be)
    host = native_mod.NativeHost.attach(4242)
    return GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes())).backpack(), be


def test_the_inventory_matches_names_from_the_cache_by_default():
    pack, _ = _backpack({0: (1511, 3), 1: (1512, 40), 2: (4151, 1)})
    # No names=: the cache's names. The note (1512) reads "Logs" as the game shows it.
    assert pack.count_named("logs") == 43
    assert pack.contains_named("WHIP") is True
    assert pack.contains_named("rune") is False
    # A caller's own resolver still wins.
    assert pack.count_named("logs", names=lambda item_id: "Oak" if item_id == 4151 else None) == 0
