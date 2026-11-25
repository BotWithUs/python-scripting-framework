"""J1 inventory name / Pattern / category / option operations, Backpack.use, and the Bank."""
import re
from dataclasses import dataclass

from botwithus.api.banking import Bank, TransferAmount, WithdrawMode
from botwithus.api.inventory import Backpack

from fake_game import FakeGame, FakeSnapshot, inv, loc, me, npc, ops

COMPONENT = 57
LOGS, OAK_LOGS, KNIFE, COINS, LOBSTER = 1511, 1521, 946, 995, 379
NAMES = {LOGS: "Logs", OAK_LOGS: "Oak logs", KNIFE: "Knife", COINS: "Coins",
         LOBSTER: "Lobster"}.get


@dataclass
class _ItemDef:
    category: int
    inventory_ops: tuple

    def has_inventory_op(self, op):
        return any(o.casefold() == op.casefold() for o in self.inventory_ops if o)


ITEM_DEFS = {LOGS: _ItemDef(22, ("", "Craft", "", "", "Drop")),
             OAK_LOGS: _ItemDef(22, ("", "Craft", "", "", "Drop")),
             KNIFE: _ItemDef(4, ("", "", "", "", "Drop")),
             COINS: _ItemDef(0, ("", "", "", "", "")),
             LOBSTER: _ItemDef(61, ("Eat", "", "", "", "Drop"))}.get

BACKPACK = inv(93, 28, {0: (KNIFE, 1), 3: (OAK_LOGS, 1), 4: (LOGS, 1), 9: (OAK_LOGS, 1),
                        12: (LOGS, 1), 20: (LOBSTER, 1), 27: (COINS, 75_000)})
BANK = inv(95, 400, {0: (COINS, 1_000_000), 5: (OAK_LOGS, 340), 6: (LOGS, 99), 7: (LOBSTER, 12)})


def _game(open_bank=True):
    snap = FakeSnapshot(inventory_rows=[BACKPACK, BANK],
                        open_ifaces={517} if open_bank else set())
    return FakeGame(snap)


# --- whole-name matching ----------------------------------------------------------------

def test_whole_names_do_not_match_substrings():
    # Failure: "Logs" matching "Oak logs" (it would deposit the wrong item).
    view = _game().backpack().view()
    assert [i.slot for i in view.matching("logs", names=NAMES)] == [4, 12]
    assert view.count_matching("Oak logs", names=NAMES) == 2
    assert view.first_exact("knife", names=NAMES).slot == 0
    assert view.first_exact("Knif", names=NAMES) is None


def test_a_pattern_must_match_the_whole_name():
    view = _game().backpack().view()
    assert [i.slot for i in view.matching(re.compile(r".*logs", re.I), names=NAMES)] \
        == [3, 4, 9, 12]
    assert view.matching(re.compile("Oak"), names=NAMES) == []


def test_contains_all_any_and_any_except():
    view = _game().backpack().view()
    assert view.contains_all_of("Logs", "Knife", names=NAMES)
    assert not view.contains_all_of("Logs", "Tinderbox", names=NAMES)
    assert view.contains_any_of("Tinderbox", "Coins", names=NAMES)
    assert view.contains_any_except(KNIFE, COINS, names=NAMES)
    assert not view.contains_any_except(KNIFE, COINS, LOBSTER, "Logs",
                                        re.compile("Oak.*"), names=NAMES)


def test_category_and_option_reads_use_the_item_definitions():
    # Failure: a category or option read from the wrong item.
    view = _game().backpack().view()
    assert [i.slot for i in view.all_in_category(22, item_defs=ITEM_DEFS)] == [3, 4, 9, 12]
    assert view.contains_category(61, item_defs=ITEM_DEFS)
    assert not view.contains_category(99, item_defs=ITEM_DEFS)
    assert [i.slot for i in view.items_with_option("eat", item_defs=ITEM_DEFS)] == [20]


def test_interact_first_and_use_click_the_whole_name_match():
    # Failure: the first substring match (slot 3, Oak logs) clicked for "Logs".
    game = _game()
    game.component_ops_map[(1473, 5, 4)] = ops("", "", "", "", "", "", "", "Drop")
    assert game.backpack().interact_first("Logs", "Drop", names=NAMES) is True
    assert game.queued == [(COMPONENT, 8, 4, (1473 << 16) | 5)]
    game.component_ops_map[(1473, 5, 0)] = ops("Use")
    assert isinstance(game.backpack(), Backpack)
    assert game.backpack().use("Knife", names=NAMES) is True
    assert game.queued[-1] == (COMPONENT, 1, 0, (1473 << 16) | 5)
    assert game.backpack().use("Tinderbox", names=NAMES) is False


# --- bank -------------------------------------------------------------------------------

def test_deposit_all_by_name_queues_one_click_per_distinct_item():
    # Failure: a second Deposit-All for the second Logs slot, or the bank grid used for a
    # backpack item.
    game = _game()
    assert Bank(game).deposit_all("Logs", LOBSTER, names=NAMES) is True
    assert game.queued == [(COMPONENT, 7, 4, (517 << 16) | 15),
                           (COMPONENT, 7, 20, (517 << 16) | 15)]


def test_deposit_all_except_keeps_the_named_items():
    game = _game()
    assert Bank(game).deposit_all_except("Knife", re.compile("Coins"), names=NAMES) is True
    assert [q[2] for q in game.queued] == [3, 4, 20]   # oak logs, logs, lobster: once each


def test_deposit_all_with_no_items_is_the_backpack_button():
    game = _game()
    assert Bank(game).deposit_all() is True
    assert game.queued == [(COMPONENT, 1, -1, (517 << 16) | 39)]


def test_nothing_is_sent_while_the_bank_is_closed():
    # Failure: a click sent with the bank closed.
    game = _game(open_bank=False)
    bank = Bank(game)
    assert not bank.deposit_all("Logs", names=NAMES)
    assert not bank.withdraw("Lobster", names=NAMES)
    assert not bank.deposit_backpack()
    assert not bank.set_withdraw_mode(WithdrawMode.NOTE)
    assert game.queued == []


def test_withdraw_by_name_and_pattern_clicks_the_bank_slot_with_the_amount_op():
    game = _game()
    bank = Bank(game)
    assert bank.withdraw("lobster", TransferAmount.TEN, names=NAMES)
    assert bank.withdraw(re.compile(r"Oak \w+"), TransferAmount.ALL, names=NAMES)
    assert game.queued == [(COMPONENT, 4, 7, (517 << 16) | 201),
                           (COMPONENT, 7, 5, (517 << 16) | 201)]


def test_set_withdraw_mode_toggles_only_when_it_differs():
    # Failure: a note toggle that flips an already-correct mode.
    game = _game()
    game.varp_values[160] = 1
    bank = Bank(game)
    assert bank.withdraw_mode() is WithdrawMode.NOTE
    assert bank.set_withdraw_mode(WithdrawMode.NOTE) is True
    assert game.queued == []
    assert bank.set_withdraw_mode(WithdrawMode.ITEM) is True
    assert game.queued == [(COMPONENT, 1, -1, (517 << 16) | 127)]


class _Defs:
    """Entity names/options by id (an EntityDefs stand-in)."""

    def __init__(self, options):
        self._options = options

    def has_option(self, entity, option):
        return self.option_slot(entity, option) is not None

    def option_slot(self, entity, option):
        names = self._options.get(entity.type_id, ())
        for i, name in enumerate(names):
            if name.casefold() == option.casefold():
                return i + 1
        return None


BOOTH, CHEST, BANKER = 10583, 79036, 3418


def test_open_prefers_a_booth_and_falls_back_to_a_banker():
    # Failure: no banker tried when no booth is in range (the Java J1 behaviour change).
    defs = _Defs({BOOTH: ("", "Bank", "Collect"), BANKER: ("Talk to", "Bank")})
    game = _game(open_bank=False)
    game.snap.loc_rows = [loc(BOOTH, 3205, 3200)]
    game.snap.npc_rows = [npc(41, BANKER, 3201, 3200)]
    assert Bank(game, defs).open() is True
    assert game.queued == [(4, BOOTH, 3205, 3200)]          # OBJECT2 = slot 2 "Bank"
    game2 = _game(open_bank=False)
    game2.snap.loc_rows = [loc(BOOTH, 3215, 3200)]           # 15 tiles: out of range
    game2.snap.npc_rows = [npc(41, BANKER, 3201, 3200)]
    assert Bank(game2, defs).open() is True
    assert game2.queued == [(10, 41, 0, 0)]                  # NPC2 = slot 2 "Bank"


def test_load_last_preset_picks_the_nearer_of_booth_and_banker():
    defs = _Defs({CHEST: ("Use", "Load Last Preset from"),
                  BANKER: ("Talk to", "Bank", "Collect", "Load Last Preset from")})
    game = _game(open_bank=False)
    game.snap.loc_rows = [loc(CHEST, 3209, 3200)]
    game.snap.npc_rows = [npc(41, BANKER, 3203, 3200)]
    assert Bank(game, defs).load_last_preset() is True
    assert game.queued == [(12, 41, 0, 0)]                   # NPC4, the nearer banker
