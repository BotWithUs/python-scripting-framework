"""N3: the gameval name index (native ABI 8) and the api modules resolving by name.

Every name below resolves to an id that differs from the module's fallback, so a module that
still reads its pinned id fails. Each test names what would count as a failure.
"""
import json
import logging
from pathlib import Path

import pytest

from botwithus.api import gameval as gv
from botwithus.api.action_bar import ActionBar, CURRENT_BAR_REF
from botwithus.api.banking import Bank
from botwithus.api.buffs import Buff, Buffs
from botwithus.api.cache_values import CacheValues
from botwithus.api.combat import LocalCombat
from botwithus.api.dialog import ChatPage, ConfirmItemDestruction
from botwithus.api.entities import SceneObjects
from botwithus.api.gameval import GamevalIndex, GamevalNotFound, GamevalRef, GamevalType
from botwithus.api.hud import Hud, HudTab
from botwithus.api.lodestones import Lodestone, LodestoneNetwork
from botwithus.api.prayers import Prayer, Prayers
from botwithus.api.quests import QuestLog
from botwithus.api.summoning import Summoning
from botwithus.core import native

from fake_game import FakeGame, FakeSnapshot, loc, me, node, ops, skill

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "j2_cache.json").read_text())


@pytest.fixture(autouse=True)
def _fresh_default():
    gv._reset_default_for_tests()
    yield
    gv._reset_default_for_tests()


class GamevalBackend:
    """The four _native.gameval_* calls over tables, counting calls."""

    def __init__(self, tables, available=True, version=1794589493):
        self.tables = {t: {n.upper(): i for n, i in names.items()} for t, names in tables.items()}
        self.available = available
        self.version = version
        self.calls = 0

    def gameval_status(self):
        self.calls += 1
        return (True, self.version, "") if self.available else (False, 0, "no local game cache")

    def gameval_id(self, wire, name):
        if wire == "mapelement":
            raise RuntimeError("gameval: the local cache has no mapelement gameval table")
        return self.tables.get(wire, {}).get(name.upper())

    def gameval_name(self, wire, id_):
        return next((n for n, i in self.tables.get(wire, {}).items() if i == id_), None)

    def gameval_prefix(self, wire, prefix, limit):
        rows = sorted((n, i) for n, i in self.tables.get(wire, {}).items() if n.startswith(prefix.upper()))
        return [(i, n) for n, i in rows[:limit]], len(rows)


def _native_index(tables, **kw):
    be = GamevalBackend(tables, **kw)
    native.set_backend(be)
    return GamevalIndex(), be


# --- the index -----------------------------------------------------------------------------

def test_native_index_resolves_both_ways_case_insensitively():
    # Failure: a case-sensitive lookup, a lost packed component, or the wrong wire string.
    index, _ = _native_index({"component": {"BANK__BANK_INV_BUTTON": (517 << 16) | 39},
                              "varbit": {"ACTIONBAR1_INT1_TYPE": 1747}})
    assert index.is_available() and index.index_version() == 1794589493
    assert index.id(GamevalType.VARBIT, "actionbar1_int1_type") == 1747
    assert index.component("bank__bank_inv_button") == (517, 39)
    assert index.gameval(GamevalType.VARBIT, 1747) == "ACTIONBAR1_INT1_TYPE"
    assert index.id(GamevalType.VARP, "ACTIONBAR1_INT1_TYPE") is None   # types are separate
    assert index.require(GamevalType.VARBIT, "ACTIONBAR1_INT1_TYPE") == 1747
    with pytest.raises(GamevalNotFound):
        index.require(GamevalType.ITEM, "NOPE")
    assert index.id(GamevalType.MAPELEMENT, "X") is None                 # a table the cache lacks


def test_starting_with_is_in_name_order_and_limited():
    index, _ = _native_index({"quest": {"COOKS_ASSISTANT": 257, "BLACK_KNIGHTS_FORTRESS": 1, "COOK_ALONE": 9}})
    rows = index.starting_with(GamevalType.QUEST, "cook", 1)   # byte order: S (0x53) before _
    assert rows == [gv.GamevalEntry(GamevalType.QUEST, 257, "COOKS_ASSISTANT")]
    with pytest.raises(ValueError):
        index.starting_with(GamevalType.QUEST, "", 0)


def test_an_older_host_is_unavailable_with_a_reason():
    # Failure: an exception, or a lookup that answers on a host without the surface.
    native.set_backend(object())
    index = GamevalIndex()
    assert not index.is_available() and "native ABI 8" in index.unavailable_reason()
    assert index.id(GamevalType.ITEM, "LOGS") is None
    assert index.starting_with(GamevalType.ITEM, "") == []


def test_unavailable_is_asked_again_only_after_the_retry_period(monkeypatch):
    # Failure: the host asked on every lookup (a loop of refs hammering a missing DLL), or never again.
    index, be = _native_index({}, available=False)
    clock = [100.0]
    monkeypatch.setattr(gv.time, "monotonic", lambda: clock[0])
    for _ in range(10):
        index.id(GamevalType.ITEM, "LOGS")
    assert be.calls == 1
    clock[0] += gv.RETRY_UNAVAILABLE_S
    be.available = True
    be.tables = {"item": {"LOGS": 1511}}
    assert index.id(GamevalType.ITEM, "LOGS") == 1511 and be.calls == 2


def test_ref_prefers_the_name_and_warns_once_on_fallback(caplog):
    # Failure: the fallback id used while the name resolves, no warning, or one per lookup.
    index = GamevalIndex.from_tables({GamevalType.VARP: {"SA_ENERGY": 9679}})
    ref = GamevalRef(GamevalType.VARP, "SA_ENERGY", 679)
    assert ref.resolve(index) == 9679
    missing = GamevalRef(GamevalType.VARP, "NOT_A_VARP", 4242)
    with caplog.at_level(logging.WARNING, logger="botwithus.api.gameval"):
        assert missing.resolve(index) == 4242
        assert missing.resolve(index) == 4242
    warnings = [r for r in caplog.records if "NOT_A_VARP" in r.getMessage()]
    assert len(warnings) == 1 and "fallback id 4242" in warnings[0].getMessage()


def test_game_api_reads_variables_by_gameval():
    # Failure: an unresolved name read as varp 0 / -1 confused, or the wrong type searched.
    from botwithus.api.game import UNRESOLVED_VARIABLE, GameApi
    _native_index({"varp": {"WOODBOX_TIER": 77}, "varbit": {"ZAROS_SPELLBOOK": 88}})

    class Reads(GameApi):
        def __init__(self):   # no connection: only the by-name forms are under test
            pass

        def varp(self, var_id):
            return var_id * 10

        def varbit(self, var_id):
            return var_id * 100

    api = Reads()
    assert api.varp_by_gameval("woodbox_tier") == 770
    assert api.varbit_by_gameval("ZAROS_SPELLBOOK") == 8800
    assert api.varp_by_gameval("ZAROS_SPELLBOOK") == UNRESOLVED_VARIABLE == -1


# --- the api modules resolve by name --------------------------------------------------------

def _bar_names(offset):
    varbits, varps, comps = {}, {}, {}
    for slot in range(1, 15):
        varbits[f"ACTIONBAR2_INT{slot}_TYPE"] = offset + 2 * slot
        varbits[f"ACTIONBAR2_INT{slot}_ID"] = offset + 2 * slot + 1
        varps[f"ACTIONBAR2_OBJ{slot}"] = offset + 500 + slot
        comps[f"TOPLEVEL_V2_COMBAT_BAR__GRAPHIC_{slot}"] = (1430 << 16) | (600 + slot)
    varbits["COMBATV2_ACTIONBAR_CURRENT_BAR"] = 99893
    return {GamevalType.VARBIT: varbits, GamevalType.VARP: varps, GamevalType.COMPONENT: comps}


def test_action_bar_reads_slot_variables_by_name():
    # Failure: the generated table read while the names resolve (ids 1771.. instead of 90002..).
    game = FakeGame(FakeSnapshot(self_player=me()))
    game.varbit_values[99893] = 2
    bar = ActionBar(game, gamevals=GamevalIndex.from_tables(_bar_names(90000)))
    assert bar.active_bar() == 2
    bar.slots()
    batches = {c[0]: c[1] for c in game.calls if c[0] in ("read_varbits", "varps")}
    assert batches["read_varbits"][:4] == [90002, 90003, 90004, 90005]
    assert batches["varps"][:2] == [90501, 90502]
    assert bar.use_slot(3) is True
    assert game.queued[-1][3] == (1430 << 16) | 603


def test_action_bar_falls_back_whole_bar_when_a_name_is_missing(caplog):
    # Failure: a mix of named and generated ids for one bar, or no warning.
    names = _bar_names(90000)
    del names[GamevalType.VARP]["ACTIONBAR2_OBJ14"]
    game = FakeGame(FakeSnapshot(self_player=me()))
    game.varbit_values[99893] = 2
    with caplog.at_level(logging.WARNING):
        ActionBar(game, gamevals=GamevalIndex.from_tables(names)).slots()
    batch = [c[1] for c in game.calls if c[0] == "read_varbits"][-1]
    assert batch[:2] == [1771, 1772]   # _action_bar_ids, ACTIONBAR2 slot 1
    assert any("generated ids" in r.getMessage() for r in caplog.records)


def test_prayers_read_their_varbits_and_struct_by_name():
    # Failure: varbit 16739 / struct 14540 / window 1458 read while their names resolve.
    names = {GamevalType.VARBIT: {"COMBATV2_PRAYER_BOOST_DEFENCE": 77739, "PRAYER_POINTS": 77736},
             GamevalType.STRUCT: {"COMBATV2_PRAYER_TIERED_DEFENCE": 14540},
             GamevalType.INTERFACE: {"TOPLEVEL_V2_PRAYER": 4458}}
    game = FakeGame(FakeSnapshot(self_player=me(skills=[skill(5, 12, 1)]), open_ifaces={4458}))
    game.varbit_values[77739] = 1
    game.varbit_values[77736] = 330
    p = Prayers(game, CacheValues.from_tables(structs={int(k): v for k, v in FIXTURES["struct"].items()}),
                gamevals=GamevalIndex.from_tables(names))
    assert p.is_active(Prayer.TIERED_DEFENCE) and p.points() == 33 and p.is_window_open()


def test_combat_and_summoning_read_by_name():
    names = {GamevalType.VARP: {"SA_ENERGY": 70679, "FOLLOWER_OBJ": 71831},
             GamevalType.VARBIT: {"LORE_POINTS": 71524}}
    index = GamevalIndex.from_tables(names)
    game = FakeGame(FakeSnapshot(self_player=me()))
    game.varp_values[70679] = 55
    game.varbit_values[71524] = 420
    assert LocalCombat(game, gamevals=index).adrenaline() == 55
    assert Summoning(game, gamevals=index).raw_points() == 420


def test_hud_and_bank_click_named_components():
    # Failure: 1431:0 / 517:39 clicked while the names resolve elsewhere.
    names = {GamevalType.COMPONENT: {"TOPLEVEL_V2_RIBBON__WINDOW_BUILD": (2431 << 16) | 5,
                                     "BANK__BANK_INV_BUTTON": (617 << 16) | 49,
                                     "BANK__BANK_INV": (617 << 16) | 301},
             GamevalType.INTERFACE: {"BANK": 617}}
    index = GamevalIndex.from_tables(names)
    game = FakeGame(FakeSnapshot(self_player=me(), open_ifaces={617}))
    game.component_ops_map[(2431, 5, 10)] = ops("Open")   # slot 10: the fallback, no enum
    assert Hud(game, CacheValues.from_tables(), gamevals=index).open(HudTab.EQUIPMENT)
    assert game.queued[-1][3] == (2431 << 16) | 5
    bank = Bank(game, gamevals=index)
    assert bank.is_open() and bank.deposit_backpack()
    assert game.queued[-1][3] == (617 << 16) | 49


def test_enum_modules_resolve_through_the_default_index():
    # Buffs, lodestones and chat pages carry their names per member. Failure: the fallback id.
    _native_index({"varbit": {"OVERLOAD_POTION_EFFECTS_LONG": 9001, "THAS_ACTIVE_VARROCK": 9002},
                   "component": {"THAS_LODESTONE_MAP__VARROCK": (1092 << 16) | 77,
                                 "CHAT_V2_LEFT__CHAT_TEXT": (1184 << 16) | 11},
                   "interface": {"CHAT_V2_LEFT": 1184}})
    assert Buff.OVERLOAD.varbit == 9001 and Buff.ANTIFIRE.varbit == 497    # 497: not named here
    assert Lodestone.VARROCK.unlock_varbit == 9002 and Lodestone.VARROCK.button == 77
    assert ChatPage.NPC.text_comp == 11 and ChatPage.NPC.continue_comp == 15
    game = FakeGame(FakeSnapshot(self_player=me()))
    game.varbit_values[9001] = 4
    assert Buffs(game).is_active(Buff.OVERLOAD)


def test_quest_names_come_from_the_gameval_listing():
    # The listing names quest 900 only; the id scan would find 0..3, 6, 132, 257, 258 from the
    # fixture. Failure: the scan used while the index lists quests.
    quests = {900: {"id": 900, "name": "Cook's Assistant", "listName": "Cook's Assistant"},
              6: {"id": 6, "name": "Cook's Assistant", "listName": ""}}
    index = GamevalIndex.from_tables({GamevalType.QUEST: {"COOKS_ASSISTANT_NEW": 900}})
    log = QuestLog(FakeGame(FakeSnapshot(self_player=me())), CacheValues.from_tables(quests=quests),
                   gamevals=index)
    assert log._listed_by_name() == {"Cook's Assistant": 900}


def test_scene_objects_filter_by_animation():
    # Loc rows carry the snapshot's animationId (on the wire since v20; on the native row since
    # ABI 8). Failure: a fire not found by its animation, or a still loc reported as animating.
    snap = FakeSnapshot(self_player=me(), loc_rows=[loc(7000, 3201, 3200, anim=2484), loc(7001, 3202, 3200)])
    burning = SceneObjects(snap).with_animation(2484).all()
    assert [o.type_id for o in burning] == [7000]
    assert [o.type_id for o in SceneObjects(snap).not_animating().all()] == [7001]


def test_confirm_destruction_presses_the_named_button():
    _native_index({"component": {"CONFIRM_DESTROY_V2__BUTTON_YES": (1183 << 16) | 17},
                   "interface": {"CONFIRM_DESTROY_V2": 1183}})
    game = FakeGame(FakeSnapshot(self_player=me(), open_ifaces={1183}))
    assert ConfirmItemDestruction(game).confirm()
    assert game.queued[-1][3] == (1183 << 16) | 17


def test_lodestone_network_window_by_name():
    _native_index({"interface": {"THAS_LODESTONE_MAP": 2092}})
    game = FakeGame(FakeSnapshot(self_player=me(), open_ifaces={2092}))
    assert LodestoneNetwork(game).is_open()


def test_current_bar_ref_is_the_java_name():
    assert (CURRENT_BAR_REF.type, CURRENT_BAR_REF.gameval) == (GamevalType.VARBIT, "COMBATV2_ACTIONBAR_CURRENT_BAR")
