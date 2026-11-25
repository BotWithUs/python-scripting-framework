"""J3 ComponentQuery / ComponentNode, Dialog chat pages, ConfirmItemDestruction, Hud, Buffs,
DepositBox and LootInventory."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from botwithus.api.buffs import Buff, Buffs
from botwithus.api.cache_values import CacheValues
from botwithus.api.component_query import ComponentQuery
from botwithus.api.deposit_box import DepositBox, LootInventory
from botwithus.api.dialog import ConfirmItemDestruction, Dialog, plain
from botwithus.api.hud import Hud, HudTab

from fake_game import FakeGame, FakeSnapshot, inv, loc, node, ops

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "j2_cache.json").read_text())
LOGS, OAK, RUNE = 1511, 1521, 1147


@dataclass
class _Item:
    name: str
    category: int


ITEMS = {LOGS: _Item("Logs", 22), OAK: _Item("Oak logs", 22), RUNE: _Item("Rune med helm", 7)}.get


def _grid_game():
    game = FakeGame()
    game.trees[(1622, 0)] = [
        node(1622, 0),
        node(1622, 9, sub=0, item=LOGS, amount=3),
        node(1622, 9, sub=1, item=OAK, amount=40),
        node(1622, 9, sub=2, item=RUNE, amount=1, visible=False),
        node(1622, 30, text="Loot All"),
    ]
    game.component_ops_map[(1622, 9, 1)] = ops("Take", "Examine")
    game.component_ops_map[(1622, 9, 0)] = ops("Take")
    game.component_ops_map[(1622, 30, -1)] = ops("Loot All", mask=0)
    return game


# --- ComponentQuery ---------------------------------------------------------------------

def test_item_filters_and_one_tree_fetch():
    # Failure: a second get_interface_tree for the second terminal, or an amount range read off
    # the wrong field.
    game = _grid_game()
    q = ComponentQuery(game, 1622, item_defs=ITEMS)
    assert [n.sub for n in q.with_item_name("logs").all()] == [0, 1]
    q2 = ComponentQuery(game, 1622, item_defs=ITEMS)
    assert [n.sub for n in q2.with_item_name(re.compile("med")).all()] == [2]
    assert [n.sub for n in ComponentQuery(game, 1622, item_defs=ITEMS)
            .with_item_category(22).with_item_amount(10, 50).all()] == [1]
    assert [n.sub for n in ComponentQuery(game, 1622).with_item_id(RUNE).hidden().all()] == [2]
    assert sum(1 for c in game.calls if c[0] == "interface_tree") == 4


def test_with_option_and_default_option_memoise_ops():
    game = _grid_game()
    q = ComponentQuery(game, 1622).with_option("Take")
    assert [(n.comp, n.sub) for n in q.all()] == [(9, 0), (9, 1)]
    assert q.count() == 2
    assert sum(1 for c in game.calls if c[0] == "component_ops") == 5   # one per node
    # "Loot All" lists op 1 with its mask bit clear: not a default (transmitting) op
    assert [n.comp for n in ComponentQuery(game, 1622).has_default_option().all()] == [9, 9]


def test_node_interact_sends_its_own_sub():
    # Failure: sub -1 sent for a grid slot (the click lands on the container).
    game = _grid_game()
    slot = ComponentQuery(game, 1622).with_item_id(OAK).first()
    assert slot.options() == ["Take", "Examine"]
    assert slot.interact("examine") is True
    assert game.queued == [(57, 2, 1, (1622 << 16) | 9)]
    assert slot.interact("Wield") is False


# --- dialog -----------------------------------------------------------------------------

def test_text_and_title_come_from_whichever_chat_page_is_open():
    # Failure: player chat text read from comp 4 (the title), or the item box read with a title.
    game = FakeGame(FakeSnapshot(open_ifaces={1191}))
    game.component_maps[(1191, 10)] = {"iface": 1191, "comp": 10, "text": "<p=1>Howdy."}
    game.component_maps[(1191, 4)] = {"iface": 1191, "comp": 4, "text": "Harness Guy"}
    d = Dialog(game)
    assert d.is_player_chat_open() and d.can_continue()
    assert (d.get_text(), d.get_title()) == ("<p=1>Howdy.", "Harness Guy")
    assert d.continue_dialog() is True
    assert game.queued == [(30, 0, -1, (1191 << 16) | 15)]
    game.snap.open_ifaces = {1189}
    game.component_maps[(1189, 3)] = {"iface": 1189, "comp": 3, "text": "You get some <col=ff0000>logs</col>."}
    assert d.get_title() is None and plain(d.get_text()) == "You get some logs ."
    d.continue_dialog()
    assert game.queued[-1] == (30, 0, -1, (1189 << 16) | 20)
    game.snap.open_ifaces = set()
    assert d.get_text() is None and not d.continue_dialog()


def test_confirm_item_destruction_presses_yes_and_no():
    # Failure: confirm pressing No (comp 8).
    game = FakeGame(FakeSnapshot(open_ifaces={1183}))
    game.component_maps[(1183, 4)] = {"iface": 1183, "comp": 4, "text": "<col=ff9040>Bronze bar"}
    c = ConfirmItemDestruction(game)
    assert c.item_name() == "Bronze bar"
    assert c.confirm() and c.cancel()
    assert game.queued == [(30, 0, -1, (1183 << 16) | 7), (30, 0, -1, (1183 << 16) | 8)]
    game.snap.open_ifaces = set()
    assert not c.confirm() and c.item_name() is None


# --- hud --------------------------------------------------------------------------------

def _hud_game(**slot_ops):
    game = FakeGame()
    for slot, listed in slot_ops.items():
        game.component_ops_map[(1431, 0, int(slot[1:]))] = listed
    return game


def _hud(game):
    return Hud(game, CacheValues.from_tables(
        enums={int(k): v for k, v in FIXTURES["enum"].items()}))


def test_hud_open_presses_the_ribbon_slot_from_the_enum_by_its_open_op():
    # RIBBON_WINDOWS: window 3 (equipment) is slot 10. Failure: the fallback or a guessed op.
    game = _hud_game(s10=ops("", "Open"))
    assert _hud(game).open(HudTab.EQUIPMENT) is True
    assert game.queued == [(57, 2, 10, (1431 << 16) | 0)]


def test_hud_already_open_sends_nothing():
    # Failure: pressing an already-front tab (it would close it).
    game = _hud_game(s9=ops("Close"))
    h = _hud(game)
    assert h.is_open(HudTab.BACKPACK) and h.open(HudTab.BACKPACK)
    assert game.queued == []
    assert not h.open(HudTab.MAGIC)          # slot lists nothing
    assert not h.is_open(HudTab.MAGIC)


def test_hud_falls_back_to_the_951_slots_without_the_enum():
    game = _hud_game(s11=ops("Open"))
    assert Hud(game, CacheValues.from_tables()).open(HudTab.PRAYER)
    assert game.queued == [(57, 1, 11, (1431 << 16) | 0)]
    Hud(game, CacheValues.from_tables()).logout()
    assert ("logout",) in game.calls


# --- buffs ------------------------------------------------------------------------------

def test_timed_buffs_count_fifteen_second_units_and_flags_have_no_time():
    # Failure: a 10 s unit, or a flag buff given a time.
    game = FakeGame()
    game.varbit_values.update({48834: 24, 43714: 3, 43696: 2, 33448: 1})
    b = Buffs(game)
    assert b.state(Buff.OVERLOAD).remaining_s == 360
    irit = b.state(Buff.IRIT_INCENSE)
    assert (irit.remaining_s, irit.potency) == (45, 2)
    agg = b.state(Buff.AGGRESSION)
    assert agg.is_active and agg.remaining_s is None
    assert set(b.active()) == {Buff.OVERLOAD, Buff.IRIT_INCENSE, Buff.AGGRESSION}


# --- deposit box / loot -----------------------------------------------------------------

NAMES = {LOGS: "Logs", OAK: "Oak logs", RUNE: "Rune med helm"}.get


def test_deposit_box_deposits_by_the_slots_live_op():
    # Failure: op 1 sent instead of the slot's own "Deposit-All" op, or a duplicate click.
    game = FakeGame(FakeSnapshot(open_ifaces={11}, inventory_rows=[
        inv(93, 28, {0: (LOGS, 1), 1: (LOGS, 1), 2: (OAK, 1), 3: (RUNE, 1)})]))
    game.component_ops_map[(11, 19, 0)] = ops("Deposit-1", "Deposit-5", "Deposit-10",
                                              "Deposit-All")
    game.component_ops_map[(11, 19, 2)] = ops("Deposit-1", "", "", "Deposit-All")
    assert DepositBox(game).deposit_all_except(RUNE) is True
    assert game.queued == [(57, 4, 0, (11 << 16) | 19), (57, 4, 2, (11 << 16) | 19)]


def test_deposit_box_buttons_are_found_by_option_and_closed_box_sends_nothing():
    game = FakeGame(FakeSnapshot(open_ifaces={11}))
    game.trees[(11, 0)] = [node(11, 0), node(11, 5), node(11, 6)]
    game.component_ops_map[(11, 6, -1)] = ops("Deposit Carried Items")
    assert DepositBox(game).deposit_all() is True
    assert game.queued == [(57, 1, -1, (11 << 16) | 6)]
    assert DepositBox(game).deposit_worn() is False
    game.snap.open_ifaces = set()
    assert DepositBox(game).deposit_all() is False


class _Defs:
    def has_option(self, e, o):
        return self.option_slot(e, o) is not None

    def option_slot(self, e, o):
        return 2 if o == "Deposit" and e.type_id == 36788 else None


def test_deposit_box_open_and_loot_take():
    game = FakeGame(FakeSnapshot(loc_rows=[loc(36788, 3205, 3205)]))
    assert DepositBox(game, _Defs()).open() is True
    assert game.queued == [(4, 36788, 3205, 3205)]

    loot_game = _grid_game()
    loot_game.snap.open_ifaces = {1622}
    loot_game.snap.inventory_rows = [inv(773, 5, {0: (LOGS, 3), 1: (OAK, 40)})]
    loot = LootInventory(loot_game)
    assert loot.take("oak logs", names=NAMES) is True
    assert loot_game.queued == [(57, 1, 1, (1622 << 16) | 9)]
    assert loot.take("Logz", names=NAMES) is False
    assert [i.item_id for i in loot.contents()] == [LOGS, OAK]
