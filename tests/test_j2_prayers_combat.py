"""J2 Prayers, LocalCombat, HitmarkHistory and Summoning."""
import json
from pathlib import Path

from botwithus.api.cache_values import CacheValues
from botwithus.api.combat import (HITMARK_ON_NPC, HITMARK_ON_PLAYER, HitmarkHistory,
                                  LocalCombat, NO_TARGET, NpcTarget, UnresolvedTarget)
from botwithus.api.events import EventBus, Hitmark
from botwithus.api.prayers import Prayer, PrayerBook, Prayers
from botwithus.api.summoning import INV_BEAST_OF_BURDEN, Summoning

from fake_game import FakeGame, FakeSnapshot, inv, me, node, npc, player, skill

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "j2_cache.json").read_text())


def _cache(**extra):
    return CacheValues.from_tables(
        enums={int(k): v for k, v in FIXTURES["enum"].items()},
        structs={int(k): v for k, v in FIXTURES["struct"].items()}, **extra)


def _prayer_game(level=12):
    return FakeGame(FakeSnapshot(self_player=me(skills=[skill(5, level, 1)]),
                                 open_ifaces={1458}))


# --- prayers ----------------------------------------------------------------------------

def test_points_are_stored_in_tenths():
    # Failure: the raw varbit (200) reported as 200 points.
    game = _prayer_game(level=37)
    game.varbit_values[16736] = 200
    p = Prayers(game, _cache())
    assert (p.raw_points(), p.points(), p.max_points()) == (200, 20, 370)


def test_tiered_prayer_names_the_tier_the_level_selects():
    # Thick Skin 1 / Rock Skin 10 / Steel Skin 28. Failure: tier 1 at level 12, or tier 3.
    p = Prayers(_prayer_game(level=12), _cache())
    info = p.info(Prayer.TIERED_DEFENCE)
    assert (info.name, info.struct_id, info.level_required) == ("Rock Skin", 14542, 10)
    assert (info.graphic_on, info.graphic_off) == (25950, 25986)
    assert Prayers(_prayer_game(level=1), _cache()).info(Prayer.TIERED_DEFENCE).name \
        == "Thick Skin"


def test_toggle_presses_the_button_showing_the_prayers_sprite():
    # Failure: a sprite-only node without triggers pressed, the wrong button, or the sub lost.
    game = _prayer_game(level=12)
    game.trees[(1458, 40)] = [
        node(1458, 40), node(1458, 41, sprite=25950, sub=-1),          # no triggers: decoration
        node(1458, 42, sub=7, sprite=25959, triggers=(1,)),            # Protect from Magic
        node(1458, 42, sub=3, sprite=25986, triggers=(1,)),            # Rock Skin (off)
    ]
    assert Prayers(game, _cache()).toggle(Prayer.TIERED_DEFENCE) is True
    assert game.queued == [(57, 1, 3, (1458 << 16) | 42)]


def test_toggle_sends_nothing_with_the_window_closed():
    game = _prayer_game()
    game.snap.open_ifaces = set()
    assert Prayers(game, _cache()).toggle(Prayer.PROTECT_MAGIC) is False
    assert game.queued == []


def test_open_window_uses_the_ribbon_enum_slot():
    # RIBBON_WINDOWS (13319) puts window 4 (prayer) at slot 11.
    game = _prayer_game()
    game.snap.open_ifaces = set()
    assert Prayers(game, _cache()).open_window() is True
    assert game.queued == [(57, 1, 11, (1431 << 16) | 0)]


def test_active_prayers_and_book():
    # Failure: the "on" value read as anything but 1, or the book read off the wrong varbit.
    game = _prayer_game()
    game.varbit_values.update({16789: 0, 16745: 1, 16747: 2, 16739: 1})
    p = Prayers(game, _cache())
    assert p.active_book() is PrayerBook.NORMAL
    assert p.is_active(Prayer.PROTECT_MAGIC) and not p.is_active(Prayer.PROTECT_MELEE)
    assert set(p.active_prayers()) == {Prayer.PROTECT_MAGIC, Prayer.TIERED_DEFENCE}
    assert p.is_any_active(Prayer.PROTECT_MELEE, Prayer.PROTECT_MAGIC)
    assert not p.is_all_active(Prayer.PROTECT_MELEE, Prayer.PROTECT_MAGIC)
    game.varbit_values[16789] = 1
    assert p.active_book() is PrayerBook.CURSES


def test_quick_prayers_press_the_orb():
    game = _prayer_game()
    game.varbit_values[5941] = 1
    p = Prayers(game, _cache())
    assert p.is_quick_prayers_active()
    p.toggle_quick_prayers()
    p.open_quick_prayer_selection()
    assert game.queued == [(57, 1, -1, (1430 << 16) | 18), (57, 2, -1, (1430 << 16) | 18)]


# --- combat -----------------------------------------------------------------------------

def test_points_and_name():
    game = FakeGame(FakeSnapshot(self_player=me(server_index=77), own_index=77,
                                 player_rows=[player(12, "Other", 1, 1),
                                              player(77, "Harness Guy", 2, 2)]))
    game.varp_values.update({679: 600, 13537: 4100, 13538: 4800})
    game.varbit_values.update({16736: 450, 41524: 330})
    combat = LocalCombat(game, Prayers(game, _cache()))
    assert combat.adrenaline() == 600
    assert (combat.life_points(), combat.max_life_points()) == (4100, 4800)
    game.health = (4000, 4900)
    assert (combat.life_points(), combat.max_life_points()) == (4000, 4900)
    assert combat.prayer_points() == 45 and combat.summoning_points() == 33
    assert combat.name() == "Harness Guy"


def test_target_resolution():
    # Failure: type 0 (cleared) or 127 (never set) read as a target, or an out-of-view NPC
    # dropped instead of reported unresolved.
    goblin = npc(31, 100, 5, 5)
    game = FakeGame(FakeSnapshot(self_player=me(target=(31, 1)), npc_rows=[goblin]))
    combat = LocalCombat(game, Prayers(game))
    assert combat.target() == NpcTarget(goblin)
    game.snap.self_player = me(target=(44, 1))
    assert combat.target() == UnresolvedTarget(44, 1)
    for cleared in (0, 127):
        game.snap.self_player = me(target=(31, cleared))
        assert combat.target() is NO_TARGET


def test_in_combat_heuristic():
    game = FakeGame(FakeSnapshot(self_player=me(server_index=77)))
    combat = LocalCombat(game, Prayers(game))
    game.varbit_values[1899] = 0
    assert not combat.is_in_combat()
    clock = [100_000]
    history = HitmarkHistory(lambda: clock[0])
    history.record(Hitmark(-1, 77, HITMARK_ON_PLAYER, 3, 120, 9))
    assert combat.is_in_combat(history)
    clock[0] += 6_001
    assert not combat.is_in_combat(history)
    game.varbit_values[1899] = 1
    assert combat.is_in_combat()


def test_hitmark_history_keys_by_kind_and_ages_out():
    # Failure: an NPC's hit counted for the player with the same index.
    bus = EventBus()
    clock = [0]
    history = HitmarkHistory.attach(bus, lambda: clock[0])
    bus.publish(Hitmark(1, 5, HITMARK_ON_NPC, 1, 40, 0))
    bus.publish(Hitmark(2, 5, HITMARK_ON_PLAYER, 1, 7, 0))
    clock[0] = 1_000
    bus.publish(Hitmark(3, 5, HITMARK_ON_NPC, 1, 60, 0))
    assert history.damage_within(HITMARK_ON_NPC, 5, 10_000) == 100
    assert history.damage_within(HITMARK_ON_PLAYER, 5, 10_000) == 7
    assert history.damage_within(HITMARK_ON_NPC, 5, 500) == 60
    clock[0] = 40_000
    assert history.recent(HITMARK_ON_NPC, 5) == []
    history.close()
    bus.publish(Hitmark(4, 5, HITMARK_ON_NPC, 1, 1, 0))
    assert history.recent(HITMARK_ON_NPC, 5, 10**9) == []


# --- summoning --------------------------------------------------------------------------

class _Item:
    name = "Pack yak pouch"


PACK_YAK_POUCH = 12093


def test_familiar_comes_from_the_pouch_params():
    # Failure: the familiar read from a hand table, or a cost/level swap.
    game = FakeGame(FakeSnapshot(self_player=me(skills=[skill(23, 96, 1)])))
    game.varp_values[1831] = PACK_YAK_POUCH
    cache = _cache(items={PACK_YAK_POUCH: _Item()},
                   item_params={PACK_YAK_POUCH: {74: 6873, 394: 96, 396: 211}})
    s = Summoning(game, cache)
    fam = s.familiar()
    assert (fam.pouch_name, fam.npc_id, fam.level_required, fam.summoning_cost) \
        == ("Pack yak pouch", 6873, 96, 211)
    assert s.max_points() == 960


def test_buttons_need_a_familiar_and_bob_is_inventory_530():
    # Failure: the beast of burden read from 671.
    game = FakeGame(FakeSnapshot(inventory_rows=[inv(530, 30, {0: (1511, 1)}),
                                                 inv(671, 30, {0: (995, 9)})]))
    s = Summoning(game, _cache())
    game.varp_values[1831] = 0
    assert not s.call() and game.queued == []
    game.varp_values[1831] = PACK_YAK_POUCH
    assert s.call() and s.dismiss() and s.cast_special()
    assert game.queued == [(57, 1, -1, (1430 << 16) | 50), (57, 1, -1, (1430 << 16) | 51),
                           (57, 1, -1, (662 << 16) | 38)]
    assert INV_BEAST_OF_BURDEN == 530
    assert [i.item_id for i in s.beast_of_burden().occupied()] == [1511]
