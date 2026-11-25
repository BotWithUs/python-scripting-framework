"""Prayer: points, the active book, which prayers are on, toggling, and quick prayers.

Mirrors the Java host's ``combat/Prayers``, ``Prayer``, ``PrayerBook`` and ``PrayerInfo``
(JBotWithUsV2, phase J2). Each :class:`Prayer` carries ``(book, struct id, active varbit id)``:
the Java host's gameval fallback ids for ``COMBATV2_PRAYER_*`` / ``CURSE_*`` (the native host
exposes no gameval index). Names, levels, drain rates and sprites come from each prayer's
struct; a tiered prayer (Thick Skin / Rock Skin / Steel Skin) names the tier the Prayer level
selects.

* Points are stored in tenths: varbit 16736 (``PRAYER_POINTS``) 200 reads 20 points.
* :meth:`Prayers.toggle` presses the prayer's button in the prayer window
  (``TOPLEVEL_V2_PRAYER__PRAYER_BUTTONS``, 1458:40), found by its on/off sprite. The window must
  be open (:meth:`Prayers.open_window`); False, with nothing sent, when it is not.
* The quick-prayer orb is ``PRAYER_GLOBE_LAYER`` (1430:18).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from .actions import component_click
from .cache_values import CacheValues, int_param, str_param
from .gameval import GamevalIndex, GamevalRef, GamevalType, component_ref
from .skills import Skill

if TYPE_CHECKING:
    from .game import GameApi

VARBIT_POINTS = 16736          # PRAYER_POINTS (tenths)
VARBIT_MODE = 16789            # PRAYER_MODE: 0 normal, 1 curses
VARBIT_QUICK_PRAYERS_ON = 5941  # TOPSTAT_PRAYER_BUTTON
PRAYER_WINDOW = 1458           # TOPLEVEL_V2_PRAYER
PRAYER_BUTTONS = 40            # TOPLEVEL_V2_PRAYER__PRAYER_BUTTONS (in 1458)
COMBAT_BAR = 1430
QUICK_PRAYER_ORB = 18          # TOPLEVEL_V2_COMBAT_BAR__PRAYER_GLOBE_LAYER (in 1430)
RIBBON = 1431                  # TOPLEVEL_V2_RIBBON__WINDOW_BUILD is 1431:0
RIBBON_BUTTONS = 0
RIBBON_WINDOWS_ENUM = 13319    # RIBBON_WINDOWS: ribbon slot -> window number
PRAYER_WINDOW_NUMBER = 4
PRAYER_RIBBON_SLOT = 11        # the 951-4 slot, when the enum cannot be read
ENUM_ALL_PRAYERS = 14185       # COMBATV2_ENUM_OF_ALL_PRAYERS
ENUM_ALL_CURSES = 14186        # COMBATV2_ENUM_OF_ALL_CURSES

# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
POINTS_REF = GamevalRef(GamevalType.VARBIT, "PRAYER_POINTS", VARBIT_POINTS)
MODE_REF = GamevalRef(GamevalType.VARBIT, "PRAYER_MODE", VARBIT_MODE)
QUICK_PRAYERS_ON_REF = GamevalRef(GamevalType.VARBIT, "TOPSTAT_PRAYER_BUTTON", VARBIT_QUICK_PRAYERS_ON)
PRAYER_WINDOW_REF = GamevalRef(GamevalType.INTERFACE, "TOPLEVEL_V2_PRAYER", PRAYER_WINDOW)
PRAYER_BUTTONS_REF = component_ref("TOPLEVEL_V2_PRAYER__PRAYER_BUTTONS", PRAYER_WINDOW, PRAYER_BUTTONS)
QUICK_PRAYER_ORB_REF = component_ref("TOPLEVEL_V2_COMBAT_BAR__PRAYER_GLOBE_LAYER", COMBAT_BAR, QUICK_PRAYER_ORB)
RIBBON_BUTTONS_REF = component_ref("TOPLEVEL_V2_RIBBON__WINDOW_BUILD", RIBBON, RIBBON_BUTTONS)
RIBBON_WINDOWS_REF = GamevalRef(GamevalType.ENUM, "RIBBON_WINDOWS", RIBBON_WINDOWS_ENUM)

PARAM_NAME = 2794
PARAM_LEVEL = 2807
PARAM_MEMBERS = 2809
PARAM_DRAIN_RATE = 2945
PARAM_QUICK_PRAYER_ID = 6670
PARAM_GRAPHIC_ON = 735
PARAM_GRAPHIC_OFF = 736
PARAM_TIER1_LEVEL = 2963       # tier n: level param 2963 + 2n, struct param 2964 + 2n
TIERS = 3

_ON = 1
_OP_TOGGLE = 1
_OP_SELECT_QUICK_PRAYERS = 2
_NO_SUB = -1
_TENTHS = 10
_POINTS_PER_LEVEL = 10
_UNKNOWN = -1


class PrayerBook(enum.Enum):
    NORMAL = 0
    CURSES = 1
    UNKNOWN = -1

    @classmethod
    def from_mode_value(cls, value: int) -> "PrayerBook":
        return next((b for b in cls if b is not cls.UNKNOWN and b.value == value), cls.UNKNOWN)

    @property
    def prayers(self) -> list["Prayer"]:
        return [p for p in Prayer if p.book is self]


class Prayer(enum.Enum):
    """A prayer or curse: ``(book, struct id, active varbit id, struct gameval, varbit gameval)``.
    The ids are the fallbacks; :attr:`struct_id` and :attr:`active_varbit` resolve the names."""

    TIERED_DEFENCE = ("normal", 14540, 16739,
        "COMBATV2_PRAYER_TIERED_DEFENCE", "COMBATV2_PRAYER_BOOST_DEFENCE")
    TIERED_MELEE_STRENGTH = ("normal", 14544, 16740,
        "COMBATV2_PRAYER_TIERED_MELEE_STRENGTH", "COMBATV2_PRAYER_BOOST_MELEE_STRENGTH")
    TIERED_MELEE_ATTACK = ("normal", 14548, 16741,
        "COMBATV2_PRAYER_TIERED_MELEE_ATTACK", "COMBATV2_PRAYER_BOOST_MELEE_ATTACK")
    RAPID_RESTORE = ("normal", 14572, 16742,
        "COMBATV2_PRAYER_RAPID_RESTORE", "COMBATV2_PRAYER_RAPID_RESTORE")
    RAPID_HEAL = ("normal", 14573, 16743,
        "COMBATV2_PRAYER_RAPID_HEAL", "COMBATV2_PRAYER_HEAL")
    PROTECT_ITEM = ("normal", 14575, 16744,
        "COMBATV2_PRAYER_PROTECT_ITEM", "COMBATV2_PRAYER_PROTECT_ITEM")
    PROTECT_MAGIC = ("normal", 14576, 16745,
        "COMBATV2_PRAYER_PROTECT_MAGIC", "COMBATV2_PRAYER_PROTECT_MAGE")
    PROTECT_MISSILES = ("normal", 14577, 16746,
        "COMBATV2_PRAYER_PROTECT_MISSILES", "COMBATV2_PRAYER_PROTECT_RANGE")
    PROTECT_MELEE = ("normal", 14578, 16747,
        "COMBATV2_PRAYER_PROTECT_MELEE", "COMBATV2_PRAYER_PROTECT_MELEE")
    PROTECT_NECROMANCY = ("normal", 48369, 53274,
        "COMBATV2_PRAYER_PROTECT_NECROMANCY", "COMBATV2_PRAYER_PROTECT_NECROMANCY")
    RETRIBUTION = ("normal", 14580, 16748,
        "COMBATV2_PRAYER_RETRIBUTION", "COMBATV2_PRAYER_RETRIBUTION")
    REDEMPTION = ("normal", 14581, 16749,
        "COMBATV2_PRAYER_REDEMPTION", "COMBATV2_PRAYER_REDEMPTION")
    SMITE = ("normal", 14582, 16750,
        "COMBATV2_PRAYER_SMITE", "COMBATV2_PRAYER_SMITE")
    TIERED_RANGE_ATTACK = ("normal", 14552, 16751,
        "COMBATV2_PRAYER_TIERED_RANGE_ATTACK", "COMBATV2_PRAYER_BOOST_RANGED_ATTACK")
    TIERED_RANGE_STRENGTH = ("normal", 14556, 16753,
        "COMBATV2_PRAYER_TIERED_RANGE_STRENGTH", "COMBATV2_PRAYER_BOOST_RANGED_STRENGTH")
    TIERED_MAGIC_ATTACK = ("normal", 14560, 16752,
        "COMBATV2_PRAYER_TIERED_MAGIC_ATTACK", "COMBATV2_PRAYER_BOOST_MAGIC_ATTACK")
    TIERED_MAGIC_STRENGTH = ("normal", 14564, 16754,
        "COMBATV2_PRAYER_TIERED_MAGIC_STRENGTH", "COMBATV2_PRAYER_BOOST_MAGIC_STRENGTH")
    TIERED_NECROMANCY_ATTACK = ("normal", 48360, 53271,
        "COMBATV2_PRAYER_TIERED_NECROMANCY_ATTACK", "COMBATV2_PRAYER_BOOST_NECROMANCY_ATTACK")
    TIERED_NECROMANCY_STRENGTH = ("normal", 48364, 53272,
        "COMBATV2_PRAYER_TIERED_NECROMANCY_STRENGTH", "COMBATV2_PRAYER_BOOST_NECROMANCY_STRENGTH")
    SUMMONING = ("normal", 14579, 16755,
        "COMBATV2_PRAYER_SUMMONING", "COMBATV2_PRAYER_PROTECT_SUMMONING")
    CHIVALRY = ("normal", 14568, 16756,
        "COMBATV2_PRAYER_CHIVALRY", "COMBATV2_PRAYER_CHIVALRY")
    PIETY = ("normal", 14569, 16757,
        "COMBATV2_PRAYER_PIETY", "COMBATV2_PRAYER_PIETY")
    RAPID_RENEWAL = ("normal", 14574, 16758,
        "COMBATV2_PRAYER_RAPID_RENEWAL", "COMBATV2_PRAYER_RAPID_RENEWAL")
    AUGURY = ("normal", 14570, 16759,
        "COMBATV2_PRAYER_AUGURY", "COMBATV2_PRAYER_AUGURY")
    RIGOUR = ("normal", 14571, 16760,
        "COMBATV2_PRAYER_RIGOUR", "COMBATV2_PRAYER_RIGOUR")
    SANCTITY = ("normal", 48368, 53273,
        "COMBATV2_PRAYER_SANCTITY", "COMBATV2_PRAYER_SANCTITY")
    DIVINE_RAGE = ("normal", 50077, 55729,
        "COMBATV2_PRAYER_DIVINE_RAGE", "COMBATV2_PRAYER_DIVINE_RAGE")
    ECLIPSED_SOUL = ("normal", 50228, 55986,
        "COMBATV2_PRAYER_ECLIPSED_SOUL", "COMBATV2_PRAYER_ECLIPSED_SOUL")
    CURSE_PROTECT_ITEM = ("curses", 14583, 16761,
        "COMBATV2_CURSE_PROTECT_ITEM", "CURSE_PROTECT_ITEM")
    CURSE_SAP_WARRIOR = ("curses", 14584, 16762,
        "COMBATV2_CURSE_SAP_WARRIOR", "CURSE_SAP_WARRIOR")
    CURSE_SAP_STRENGTH = ("curses", 14591, 16787,
        "COMBATV2_CURSE_SAP_STRENGTH", "CURSE_SAP_MELEE_STR")
    CURSE_SAP_RANGER = ("curses", 14585, 16763,
        "COMBATV2_CURSE_SAP_RANGER", "CURSE_SAP_RANGER")
    CURSE_SAP_RANGE_STRENGTH = ("curses", 14586, 16786,
        "COMBATV2_CURSE_SAP_RANGE_STRENGTH", "CURSE_SAP_RANGE_STR")
    CURSE_SAP_MAGE = ("curses", 14587, 16764,
        "COMBATV2_CURSE_SAP_MAGE", "CURSE_SAP_MAGE")
    CURSE_SAP_MAGIC_STRENGTH = ("curses", 14588, 16785,
        "COMBATV2_CURSE_SAP_MAGIC_STRENGTH", "CURSE_SAP_MAGIC_STR")
    CURSE_SAP_NECROMANCY_ATTACK = ("curses", 48370, 53275,
        "COMBATV2_CURSE_SAP_NECROMANCY_ATTACK", "CURSE_SAP_NECROMANCY_ATTACK")
    CURSE_SAP_NECROMANCY_STRENGTH = ("curses", 48371, 53276,
        "COMBATV2_CURSE_SAP_NECROMANCY_STRENGTH", "CURSE_SAP_NECROMANCY_STRENGTH")
    CURSE_SAP_DEFENCE = ("curses", 14590, 16788,
        "COMBATV2_CURSE_SAP_DEFENCE", "CURSE_SAP_DEFENCE")
    CURSE_SAP_SPIRIT = ("curses", 14589, 16765,
        "COMBATV2_CURSE_SAP_SPIRIT", "CURSE_SAP_SPIRIT")
    CURSE_BERSERKER = ("curses", 14592, 16766,
        "COMBATV2_CURSE_BERSERKER", "CURSE_BERSERKER")
    CURSE_DEFLECT_SUMMONING = ("curses", 14593, 16767,
        "COMBATV2_CURSE_DEFLECT_SUMMONING", "CURSE_DEFLECT_SUMMONING")
    CURSE_DEFLECT_MAGIC = ("curses", 14594, 16768,
        "COMBATV2_CURSE_DEFLECT_MAGIC", "CURSE_DEFLECT_MAGIC")
    CURSE_DEFLECT_MISSILES = ("curses", 14595, 16769,
        "COMBATV2_CURSE_DEFLECT_MISSILES", "CURSE_DEFLECT_RANGED")
    CURSE_DEFLECT_MELEE = ("curses", 14596, 16770,
        "COMBATV2_CURSE_DEFLECT_MELEE", "CURSE_DEFLECT_MELEE")
    CURSE_DEFLECT_NECROMANCY = ("curses", 48372, 53281,
        "COMBATV2_CURSE_DEFLECT_NECROMANCY", "CURSE_DEFLECT_NECROMANCY")
    CURSE_LEECH_ATTACK = ("curses", 14597, 16771,
        "COMBATV2_CURSE_LEECH_ATTACK", "CURSE_LEECH_ATTACK")
    CURSE_LEECH_RANGED = ("curses", 14598, 16772,
        "COMBATV2_CURSE_LEECH_RANGED", "CURSE_LEECH_RANGED")
    CURSE_LEECH_RANGE_STRENGTH = ("curses", 14599, 16781,
        "COMBATV2_CURSE_LEECH_RANGE_STRENGTH", "CURSE_LEECH_RANGED_STRENGTH")
    CURSE_LEECH_MAGIC = ("curses", 14600, 16773,
        "COMBATV2_CURSE_LEECH_MAGIC", "CURSE_LEECH_MAGIC")
    CURSE_LEECH_MAGIC_STRENGTH = ("curses", 14601, 16782,
        "COMBATV2_CURSE_LEECH_MAGIC_STRENGTH", "CURSE_LEECH_MAGIC_STRENGTH")
    CURSE_LEECH_NECROMANCY_ATTACK = ("curses", 48373, 53277,
        "COMBATV2_CURSE_LEECH_NECROMANCY_ATTACK", "CURSE_LEECH_NECROMANCY_ATTACK")
    CURSE_LEECH_NECROMANCY_STRENGTH = ("curses", 48374, 53278,
        "COMBATV2_CURSE_LEECH_NECROMANCY_STRENGTH", "CURSE_LEECH_NECROMANCY_STRENGTH")
    CURSE_LEECH_DEFENCE = ("curses", 14602, 16774,
        "COMBATV2_CURSE_LEECH_DEFENCE", "CURSE_LEECH_DEFENCE")
    CURSE_LEECH_STRENGTH = ("curses", 14603, 16775,
        "COMBATV2_CURSE_LEECH_STRENGTH", "CURSE_LEECH_STRENGTH")
    CURSE_LEECH_ENERGY = ("curses", 14604, 16776,
        "COMBATV2_CURSE_LEECH_ENERGY", "CURSE_LEECH_ENERGY")
    CURSE_LEECH_SPECIAL_ATTACK = ("curses", 14605, 16777,
        "COMBATV2_CURSE_LEECH_SPECIAL_ATTACK", "CURSE_LEECH_SPECIAL_ATTACK")
    CURSE_WRATH = ("curses", 14606, 16778,
        "COMBATV2_CURSE_WRATH", "CURSE_WRATH")
    CURSE_SOULSPLIT = ("curses", 14607, 16779,
        "COMBATV2_CURSE_SOULSPLIT", "CURSE_SOULSPLIT")
    CURSE_TURMOIL = ("curses", 14608, 16780,
        "COMBATV2_CURSE_TURMOIL", "CURSE_TURMOIL")
    CURSE_TORMENT = ("curses", 14609, 16784,
        "COMBATV2_CURSE_TORMENT", "CURSE_TORMENT")
    CURSE_ANGUISH = ("curses", 14610, 16783,
        "COMBATV2_CURSE_ANGUISH", "CURSE_ANGUISH")
    CURSE_SORROW = ("curses", 48375, 53279,
        "COMBATV2_CURSE_SORROW", "CURSE_SORROW")
    CURSE_FORTITUDE = ("curses", 32272, 29065,
        "COMBATV2_CURSE_FORTITUDE", "CURSE_FORTITUDE")
    CURSE_LIGHTFORM = ("curses", 32273, 29066,
        "COMBATV2_CURSE_LIGHTFORM", "CURSE_LIGHTFORM")
    CURSE_DARKFORM = ("curses", 32274, 29067,
        "COMBATV2_CURSE_DARKFORM", "CURSE_DARKFORM")
    CURSE_SOULLINK = ("curses", 32275, 29068,
        "COMBATV2_CURSE_SOULLINK", "CURSE_SOULLINK")
    CURSE_TEAMWORK_PROTECTION = ("curses", 32276, 29069,
        "COMBATV2_CURSE_TEAMWORK_PROTECTION", "CURSE_TEAMWORK_PROTECTION")
    CURSE_CHRONICLE_ATTRACTION = ("curses", 29241, 49330,
        "COMBATV2_CURSE_CHRONICLE_ATTRACTION", "CURSE_CHRONICLE_ATTRACTION")
    CURSE_SUPERHEAT_FORM = ("curses", 32278, 29071,
        "COMBATV2_CURSE_SUPERHEAT_FORM", "CURSE_SUPERHEAT_FORM")
    CURSE_OVERHEAD_TURMOIL = ("curses", 35360, 34866,
        "COMBATV2_CURSE_OVERHEAD_TURMOIL", "CURSE_OVERHEAD_TURMOIL")
    CURSE_OVERHEAD_ANGUISH = ("curses", 35361, 34867,
        "COMBATV2_CURSE_OVERHEAD_ANGUISH", "CURSE_OVERHEAD_ANGUISH")
    CURSE_OVERHEAD_TORMENT = ("curses", 35362, 34868,
        "COMBATV2_CURSE_OVERHEAD_TORMENT", "CURSE_OVERHEAD_TORMENT")
    CURSE_RUINATION = ("curses", 48376, 53280,
        "COMBATV2_CURSE_RUINATION", "CURSE_RUINATION")
    @property
    def book(self) -> PrayerBook:
        return PrayerBook.CURSES if self.value[0] == "curses" else PrayerBook.NORMAL

    @property
    def struct_ref(self) -> GamevalRef:
        return GamevalRef(GamevalType.STRUCT, self.value[3], self.value[1])

    @property
    def varbit_ref(self) -> GamevalRef:
        return GamevalRef(GamevalType.VARBIT, self.value[4], self.value[2])

    @property
    def struct_id(self) -> int:
        """The struct, by its gameval name; the fallback id when the name does not resolve."""
        return self.struct_ref.resolve()

    @property
    def active_varbit(self) -> int:
        """The on/off varbit, by its gameval name; the fallback id when it does not resolve."""
        return self.varbit_ref.resolve()


@dataclass(frozen=True, slots=True)
class PrayerInfo:
    """A prayer's cache data (the tier the Prayer level selects, for a tiered prayer)."""

    prayer: Prayer
    struct_id: int
    name: str
    level_required: int
    drain_rate: int
    is_members: bool
    quick_prayer_id: int
    graphic_on: int
    graphic_off: int

    @classmethod
    def from_struct(cls, prayer: Prayer, struct_id: int,
                    params: dict[int, Any]) -> "PrayerInfo":
        def value(param: int) -> int:
            found = int_param(params, param)
            return _UNKNOWN if found is None else found
        return cls(prayer, struct_id, str_param(params, PARAM_NAME) or prayer.name,
                   value(PARAM_LEVEL), value(PARAM_DRAIN_RATE),
                   (int_param(params, PARAM_MEMBERS) or 0) != 0, value(PARAM_QUICK_PRAYER_ID),
                   value(PARAM_GRAPHIC_ON), value(PARAM_GRAPHIC_OFF))


class Prayers:
    """Prayer reads and clicks for one game connection."""

    def __init__(self, game: "GameApi", cache: CacheValues | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._cache = cache or CacheValues()
        self._gamevals = gamevals

    def _id(self, ref: GamevalRef) -> int:
        return ref.resolve(self._gamevals)

    # --- points and state -------------------------------------------------------------

    def raw_points(self) -> int:
        """The varbit as stored: tenths of a point."""
        return self._game.varbit(self._id(POINTS_REF))

    def points(self) -> int:
        raw = self.raw_points()
        return -1 if raw < 0 else raw // _TENTHS

    def max_points(self) -> int:
        level = _skill_level(self._game, Skill.PRAYER)
        return -1 if level < 0 else level * _POINTS_PER_LEVEL

    def active_book(self) -> PrayerBook:
        return PrayerBook.from_mode_value(self._game.varbit(self._id(MODE_REF)))

    def is_active(self, prayer: Prayer) -> bool:
        return self._game.varbit(self._id(prayer.varbit_ref)) == _ON

    def is_any_active(self, *prayers: Prayer) -> bool:
        return bool(self._active(prayers))

    def is_all_active(self, *prayers: Prayer) -> bool:
        return len(self._active(prayers)) == len(prayers)

    def active_prayers(self) -> list[Prayer]:
        return self._active(self.active_book().prayers)

    def info(self, prayer: Prayer) -> PrayerInfo | None:
        base = self._id(prayer.struct_ref)
        params = self._cache.struct_params(base)
        if params is None:
            return None
        struct_id, chosen = self._tier_for(params) or (base, params)
        return PrayerInfo.from_struct(prayer, struct_id, chosen)

    # --- clicks -----------------------------------------------------------------------

    def is_window_open(self) -> bool:
        return self._game.snapshot().is_interface_open(self._id(PRAYER_WINDOW_REF))

    def open_window(self) -> bool:
        """Open the prayer window through its ribbon slot. True when already open."""
        if self.is_window_open():
            return True
        ribbon, buttons = RIBBON_BUTTONS_REF.component(self._gamevals)
        self._game.queue_action(component_click(ribbon, buttons, option=_OP_TOGGLE,
                                                sub_slot=self._ribbon_slot()))
        return True

    def toggle(self, prayer: Prayer) -> bool:
        """Press ``prayer``'s button. False, with nothing sent, when the window is closed, the
        prayer's cache data cannot be read, or no button shows its sprite."""
        info = self.info(prayer)
        if info is None or not self.is_window_open():
            return False
        button = self._find_button(info)
        if button is None:
            return False
        self._game.queue_action(component_click(int(button["iface"]), int(button["comp"]),
                                                option=_OP_TOGGLE, sub_slot=int(button["sub"])))
        return True

    def is_quick_prayers_active(self) -> bool:
        return self._game.varbit(self._id(QUICK_PRAYERS_ON_REF)) == _ON

    def toggle_quick_prayers(self) -> bool:
        bar, orb = QUICK_PRAYER_ORB_REF.component(self._gamevals)
        self._game.queue_action(component_click(bar, orb, option=_OP_TOGGLE))
        return True

    def open_quick_prayer_selection(self) -> bool:
        bar, orb = QUICK_PRAYER_ORB_REF.component(self._gamevals)
        self._game.queue_action(component_click(bar, orb, option=_OP_SELECT_QUICK_PRAYERS))
        return True

    # --- helpers ----------------------------------------------------------------------

    def _active(self, prayers: Iterable[Prayer]) -> list[Prayer]:
        prayers = list(prayers)
        if not prayers:
            return []
        reads = self._game.read_varbits([self._id(p.varbit_ref) for p in prayers])
        return [p for p, r in zip(prayers, reads) if r.value == _ON]

    def _tier_for(self, tiered: dict[int, Any]) -> tuple[int, dict[int, Any]] | None:
        level = max(0, _skill_level(self._game, Skill.PRAYER))
        chosen: tuple[int, dict[int, Any]] | None = None
        for tier in range(TIERS):
            needed = int_param(tiered, PARAM_TIER1_LEVEL + 2 * tier)
            struct_id = int_param(tiered, PARAM_TIER1_LEVEL + 2 * tier + 1)
            if needed is None or struct_id is None:
                break
            if chosen is None or level >= needed:
                params = self._cache.struct_params(struct_id)
                chosen = None if params is None else (struct_id, params)
        return chosen

    def _ribbon_slot(self) -> int:
        windows = self._cache.enum_entries(self._id(RIBBON_WINDOWS_REF)) or {}
        return next((slot for slot, window in windows.items() if window == PRAYER_WINDOW_NUMBER),
                    PRAYER_RIBBON_SLOT)

    def _find_button(self, info: PrayerInfo) -> dict[str, Any] | None:
        tree = self._game.interface_tree(*PRAYER_BUTTONS_REF.component(self._gamevals))
        nodes = tree.get("nodes") if isinstance(tree, dict) else None
        for node in nodes or ():
            if (node.get("sprite_id") in (info.graphic_on, info.graphic_off)
                    and node.get("triggers")):
                return node
        return None


def _skill_level(game: "GameApi", skill: Skill) -> int:
    """The local player's actual level in ``skill``, or -1 when unknown."""
    player = game.snapshot().self_player
    if not player.valid:
        return -1
    return next((s.actual_level for s in player.skills if s.type_id == int(skill)), -1)
