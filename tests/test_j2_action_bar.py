"""J2 ActionBar: slot decode, the ability catalog, the 8247 -> 6506 cooldown join, units."""
import json
from pathlib import Path

from botwithus.api import _action_bar_ids as ids
from botwithus.api.action_bar import (AbilityCatalog, AbilitySlot, ActionBar, Cooldown,
                                      EmptySlot, ItemSlot, UnresolvedSlot)
from botwithus.api.cache_values import CacheValues
from botwithus.core.rpc import RpcError

from fake_game import FakeGame, FakeSnapshot

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "j2_cache.json").read_text())
BACKHAND_STRUCT = 14682
MELEE_TYPE = 1          # COMBATV2_TYPE_TO_ENUM 1 -> enum 10147, whose key 3 is Backhand
BACKHAND_SLOT_ID = 3


def _cache():
    return CacheValues.from_tables(
        enums={int(k): v for k, v in FIXTURES["enum"].items()},
        structs={int(k): v for k, v in FIXTURES["struct"].items()})


def _bar(game, bar=3):
    game.varbit_values[1893] = bar
    return ActionBar(game, AbilityCatalog(_cache()))


def _fill(game, bar, slot, type_code, slot_id, item=0):
    type_bit, id_bit, obj_varp = ids.SLOT_VARS[bar - 1][slot - 1]
    game.varbit_values[type_bit] = type_code
    game.varbit_values[id_bit] = slot_id
    game.varp_values[obj_varp] = item


def test_slot_ids_match_the_java_live_note():
    # The Java J2 live run pressed slot 1 at 93716546 = (1430 << 16) | 66; bar 1 slot 1 reads
    # varbits 1747/1748 and varp 823.
    assert ids.SLOT_BUTTONS[0] == (1430 << 16) | 66
    assert ids.SLOT_VARS[0][0] == (1747, 1748, 823)
    assert len(ids.SLOT_VARS) == 18 and all(len(b) == 14 for b in ids.SLOT_VARS)


def test_slots_decode_ability_item_empty_and_unresolved():
    # Failure: type and id varbits read the wrong way round (slot 2 would be Unresolved), or an
    # item slot read as an ability.
    game = FakeGame()
    _fill(game, 3, 2, MELEE_TYPE, BACKHAND_SLOT_ID)
    _fill(game, 3, 5, 0, 0, item=385)
    _fill(game, 3, 9, MELEE_TYPE, 999)
    slots = _bar(game).slots()
    assert len(slots) == 14
    assert slots[0] == EmptySlot(1)
    assert isinstance(slots[1], AbilitySlot) and slots[1].ability.name == "Backhand"
    assert slots[1].ability.struct_id == BACKHAND_STRUCT and slots[1].ability.cooldown_ticks == 25
    assert slots[4] == ItemSlot(5, 385)
    assert slots[8] == UnresolvedSlot(9, MELEE_TYPE, 999)
    # one batch per bar: the type/id varbits in slot order, then the item varps
    bar3 = ids.SLOT_VARS[2]
    assert ("read_varbits", [v for t, i, _ in bar3 for v in (t, i)]) in game.calls
    assert ("varps", [o for _, _, o in bar3]) in game.calls


def test_find_and_use_ability_presses_its_slot_button():
    game = FakeGame()
    _fill(game, 3, 6, MELEE_TYPE, BACKHAND_SLOT_ID)
    bar = _bar(game)
    assert bar.contains_ability("backhand")
    assert bar.use_ability("BACKHAND") is True
    assert game.queued == [(57, 1, -1, ids.SLOT_BUTTONS[5])]
    assert not bar.use_ability("Slice") and not bar.use_slot(15)


def test_no_active_bar_reads_empty():
    game = FakeGame()
    game.varbit_values[1893] = 0
    assert ActionBar(game, AbilityCatalog(_cache())).slots() == []


def test_cooldown_joins_through_8247_then_6506_and_releases_both_handles():
    # Failure: the join done by name, the second script fed the ability struct instead of the
    # first script's answer, or a handle left alive.
    game = FakeGame()
    game.scripts[8247] = lambda args: [70001] if args == [BACKHAND_STRUCT] else [0]
    game.scripts[6506] = lambda args: [17764, 17854] if args == [70001] else [0, 0]
    bar = _bar(game)
    cd = bar.cooldown("Backhand")
    assert cd == Cooldown(17764, 17854)
    assert ("execute_script", 8247, [BACKHAND_STRUCT], ["int"]) in game.calls
    assert ("execute_script", 6506, [70001], ["int", "int"]) in game.calls
    assert sorted(game.destroyed) == [6506, 8247]


def test_cooldown_is_in_client_cycles():
    # The 3-tick global cooldown is 90 cycles (3 x 30): 60 cycles left = 1200 ms.
    game = FakeGame(FakeSnapshot(game_cycle=17794))
    game.varc_values.update({2091: 17764, 2092: 17854})
    bar = _bar(game)
    gcd = bar.global_cooldown()
    assert gcd.end_cycle - gcd.start_cycle == 90
    assert gcd.remaining_cycles(17794) == 60 and gcd.remaining_ms(17794) == 1200
    assert not gcd.is_ready(17794) and gcd.is_ready(17854)


def test_a_failing_script_reads_unknown_never_ready():
    # Failure: a cooldown that reports "ready" when the scripts failed.
    game = FakeGame()
    game.scripts[8247] = RpcError("execute_script", "execute_script: failed")
    bar = _bar(game)
    cd = bar.cooldown("Backhand")
    assert cd is Cooldown.UNKNOWN and not cd.is_ready(10**9)
    assert cd.remaining_ms(0) == -1
    assert game.destroyed == [8247]
    game2 = FakeGame()                     # no handle at all
    assert _bar(game2).cooldown("Backhand") is Cooldown.UNKNOWN
    game3 = FakeGame()                     # second script answers short
    game3.scripts[8247] = lambda a: [5]
    game3.scripts[6506] = lambda a: [1]
    assert _bar(game3).cooldown("Backhand") is Cooldown.UNKNOWN
    assert bar.cooldown("Not an ability") is Cooldown.UNKNOWN


def test_catalog_does_not_memoise_a_missing_cache():
    tables = {"enum": {}, "struct": {}}
    cache = CacheValues(lambda kind, i: tables[kind].get(i))
    catalog = AbilityCatalog(cache)
    assert catalog.resolve(MELEE_TYPE, BACKHAND_SLOT_ID) is None
    tables["enum"] = {int(k): v for k, v in FIXTURES["enum"].items()}
    tables["struct"] = {int(k): v for k, v in FIXTURES["struct"].items()}
    cache._memo.clear()
    assert catalog.resolve(MELEE_TYPE, BACKHAND_SLOT_ID).name == "Backhand"
