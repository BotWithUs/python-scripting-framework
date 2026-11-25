"""The HUD ribbon tabs (skills, backpack, equipment, prayer, magic), logout and exit to lobby.

Mirrors the Java host's ``hud/Hud`` and ``HudTab`` (JBotWithUsV2, phase J3), including what its
live run found:

* Every ribbon button's option is plain **"Open"**, or **"Close"** while its window is in front,
  not "Open Backpack". A docked window stays loaded behind another tab, so "is the interface
  open" says nothing; the button's own Open/Close option is the state.
* Each tab's ribbon slot comes from the cache enum ``RIBBON_WINDOWS`` (13319, slot -> window);
  the 951-4 slots are the fallback when the enum cannot be read.
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from .actions import component_click
from .cache_values import CacheValues
from .components import ComponentOps
from .gameval import GamevalIndex, GamevalRef, GamevalType, component_ref

if TYPE_CHECKING:
    from .game import GameApi

RIBBON = 1431
RIBBON_BUTTONS = 0             # TOPLEVEL_V2_RIBBON__WINDOW_BUILD
RIBBON_WINDOWS_ENUM = 13319    # RIBBON_WINDOWS
# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
RIBBON_BUTTONS_REF = component_ref("TOPLEVEL_V2_RIBBON__WINDOW_BUILD", RIBBON, RIBBON_BUTTONS)
RIBBON_WINDOWS_REF = GamevalRef(GamevalType.ENUM, "RIBBON_WINDOWS", RIBBON_WINDOWS_ENUM)
OPEN = "Open"
CLOSE = "Close"


class HudTab(enum.Enum):
    """``(window number, fallback ribbon slot)``."""

    SKILLS = (0, 8)
    BACKPACK = (2, 9)
    EQUIPMENT = (3, 10)
    PRAYER = (4, 11)
    MAGIC = (5, 12)

    @property
    def window(self) -> int:
        return self.value[0]

    @property
    def fallback_slot(self) -> int:
        return self.value[1]


class Hud:
    def __init__(self, game: "GameApi", cache: CacheValues | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._cache = cache or CacheValues()
        self._gamevals = gamevals

    def is_open(self, tab: HudTab) -> bool:
        """The tab's window is in front: its ribbon button lists "Close"."""
        return self._button_ops(tab).op_named(CLOSE) is not None

    def open(self, tab: HudTab) -> bool:
        """Press the tab's ribbon button by its "Open" option. True, with nothing sent, when the
        window is already in front; False when the button lists neither option."""
        ops = self._button_ops(tab)
        if ops.op_named(CLOSE) is not None:
            return True
        op = ops.op_named(OPEN)
        if op is None:
            return False
        ribbon, buttons = RIBBON_BUTTONS_REF.component(self._gamevals)
        self._game.queue_action(component_click(ribbon, buttons, option=op, sub_slot=self.slot_of(tab)))
        return True

    def logout(self) -> bool:
        return self._game.logout()

    def exit_to_lobby(self) -> bool:
        return self._game.exit_to_lobby()

    def slot_of(self, tab: HudTab) -> int:
        windows = self._cache.enum_entries(RIBBON_WINDOWS_REF.resolve(self._gamevals)) or {}
        return next((slot for slot, window in windows.items() if window == tab.window),
                    tab.fallback_slot)

    def _button_ops(self, tab: HudTab) -> ComponentOps:
        from ..core.rpc import RpcError
        try:
            ribbon, buttons = RIBBON_BUTTONS_REF.component(self._gamevals)
            return self._game.component_ops(ribbon, buttons, self.slot_of(tab))
        except RpcError:
            return ComponentOps(0, ())
