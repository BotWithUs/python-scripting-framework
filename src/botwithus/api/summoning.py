"""Summoning: points, the familiar out now (from its pouch's cache params), timers, the
combat-bar familiar buttons, and the beast-of-burden inventory.

Mirrors the Java host's ``summoning/Summoning``, ``Familiar`` and ``BeastOfBurden`` (JBotWithUsV2,
phase J2). The beast-of-burden inventory is **530** (``LORE_BOB``), not 671 (which the gameval
index names ``WORN_DISPLAY_PREVIEW``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import component_click
from .cache_values import CacheValues, int_param
from .gameval import GamevalIndex, GamevalRef, GamevalType, component_ref
from .inventory import InventoryView
from .prayers import _skill_level
from .skills import Skill

if TYPE_CHECKING:
    from .game import GameApi

INV_BEAST_OF_BURDEN = 530      # LORE_BOB
VARBIT_POINTS = 41524          # LORE_POINTS (tenths)
VARP_POUCH = 1831              # FOLLOWER_OBJ: the pouch item of the familiar out, 0 none
VARBIT_MINUTES_LEFT = 6055     # LORE_FULL_TIMER
VARP_SPECIAL_POINTS = 1787     # LORE_SPELL_POINTS
VARBIT_SCROLLS_STORED = 25412  # COMBATV2_SUMMONING_SCROLLCOUNT
COMBAT_BAR = 1430
CALL_BUTTON = 50               # TOPLEVEL_V2_COMBAT_BAR__CALL_OP1
DISMISS_BUTTON = 51            # TOPLEVEL_V2_COMBAT_BAR__DISMISS_OP1
INTERACT_BUTTON = 44           # TOPLEVEL_V2_COMBAT_BAR__INTERACT_OP1
FAMILIAR_SIDE = 662
SPECIAL_BUTTON = 38            # LORE_STATS_SIDE__LORE_SPELL_BUTTON_GRAPHIC_1 (in 662)
# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
# LORE_BOB (530) stays an id: the Java host does not name it either.
POINTS_REF = GamevalRef(GamevalType.VARBIT, "LORE_POINTS", VARBIT_POINTS)
POUCH_REF = GamevalRef(GamevalType.VARP, "FOLLOWER_OBJ", VARP_POUCH)
MINUTES_LEFT_REF = GamevalRef(GamevalType.VARBIT, "LORE_FULL_TIMER", VARBIT_MINUTES_LEFT)
SPECIAL_POINTS_REF = GamevalRef(GamevalType.VARP, "LORE_SPELL_POINTS", VARP_SPECIAL_POINTS)
SCROLLS_STORED_REF = GamevalRef(GamevalType.VARBIT, "COMBATV2_SUMMONING_SCROLLCOUNT", VARBIT_SCROLLS_STORED)
CALL_REF = component_ref("TOPLEVEL_V2_COMBAT_BAR__CALL_OP1", COMBAT_BAR, CALL_BUTTON)
DISMISS_REF = component_ref("TOPLEVEL_V2_COMBAT_BAR__DISMISS_OP1", COMBAT_BAR, DISMISS_BUTTON)
INTERACT_REF = component_ref("TOPLEVEL_V2_COMBAT_BAR__INTERACT_OP1", COMBAT_BAR, INTERACT_BUTTON)
SPECIAL_REF = component_ref("LORE_STATS_SIDE__LORE_SPELL_BUTTON_GRAPHIC_1", FAMILIAR_SIDE, SPECIAL_BUTTON)

PARAM_FOLLOWER_NPC = 74
PARAM_LEVEL_REQUIRED = 394
PARAM_COST = 396

_NO_POUCH = 0
_OP_PRESS = 1
_TENTHS = 10
_POINTS_PER_LEVEL = 10
_UNKNOWN = -1


@dataclass(frozen=True, slots=True)
class Familiar:
    """The familiar a pouch summons, read from the pouch item's params."""

    pouch_item_id: int
    pouch_name: str
    npc_id: int
    level_required: int
    summoning_cost: int


class Summoning:
    """Summoning reads and familiar buttons for one game connection."""

    def __init__(self, game: "GameApi", cache: CacheValues | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._cache = cache or CacheValues()
        self._gamevals = gamevals

    def raw_points(self) -> int:
        return self._game.varbit(POINTS_REF.resolve(self._gamevals))

    def points(self) -> int:
        raw = self.raw_points()
        return -1 if raw < 0 else raw // _TENTHS

    def max_points(self) -> int:
        level = _skill_level(self._game, Skill.SUMMONING)
        return -1 if level < 0 else level * _POINTS_PER_LEVEL

    def pouch_id(self) -> int:
        """The pouch item of the familiar out now; 0 with none."""
        return max(self._game.varp(POUCH_REF.resolve(self._gamevals)), _NO_POUCH)

    def has_familiar(self) -> bool:
        return self.pouch_id() > _NO_POUCH

    def familiar(self) -> Familiar | None:
        pouch = self.pouch_id()
        if pouch == _NO_POUCH:
            return None
        params = self._cache.item_params(pouch)
        npc = int_param(params, PARAM_FOLLOWER_NPC)
        if npc is None:
            return None
        item = self._cache.item(pouch)
        level = int_param(params, PARAM_LEVEL_REQUIRED)
        cost = int_param(params, PARAM_COST)
        return Familiar(pouch, item.name if item is not None else "", npc,
                        _UNKNOWN if level is None else level, _UNKNOWN if cost is None else cost)

    def minutes_remaining(self) -> int:
        return max(0, self._game.varbit(MINUTES_LEFT_REF.resolve(self._gamevals)))

    def special_move_points(self) -> int:
        return self._game.varp(SPECIAL_POINTS_REF.resolve(self._gamevals))

    def scrolls_stored(self) -> int:
        return max(0, self._game.varbit(SCROLLS_STORED_REF.resolve(self._gamevals)))

    def beast_of_burden(self) -> InventoryView:
        """The familiar's carried items (inv 530); not present without a beast of burden."""
        return InventoryView(self._game.snapshot().inventory(INV_BEAST_OF_BURDEN))

    def call(self) -> bool:
        return self._press(*CALL_REF.component(self._gamevals))

    def dismiss(self) -> bool:
        return self._press(*DISMISS_REF.component(self._gamevals))

    def interact(self) -> bool:
        return self._press(*INTERACT_REF.component(self._gamevals))

    def cast_special(self) -> bool:
        return self._press(*SPECIAL_REF.component(self._gamevals))

    def _press(self, iface: int, comp: int) -> bool:
        """False, with nothing sent, when no familiar is out."""
        if not self.has_familiar():
            return False
        self._game.queue_action(component_click(iface, comp, option=_OP_PRESS))
        return True
