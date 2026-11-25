"""The deposit box and the loot window.

Mirrors the Java host's ``inventory/DepositBox`` and ``LootInventory`` (JBotWithUsV2, phase J3).
Buttons are pressed by their live option name, found in the window's component tree, so a wrong
name answers False rather than clicking something else. The option names are xapi's and were not
verified live in the Java host either (the harness character stood at no deposit box, and Area
Loot was off).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from ..core.rpc import RpcError
from .actions import component_click
from .component_query import ComponentQuery
from .entities import GroundItems, SceneObjects
from .gameval import GamevalRef, GamevalType, component_ref
from .interact import EntityDefs, interact
from .inventory import InventoryView, ItemNames, NameSpec, _keep_test
from .snapshot_types import InventoryItem

if TYPE_CHECKING:
    from .game import GameApi

DEPOSIT_BOX_IFACE = 11         # BANK_DEPOSIT_BOX
DEPOSIT_BOX_GRID = 19          # BANK_DEPOSIT_BOX__ICONS
OPEN = "Deposit"
DEPOSIT_CARRIED = "Deposit Carried Items"
DEPOSIT_WORN = "Deposit Worn Items"
DEPOSIT_FAMILIAR = "Deposit Familiar's Items"
DEPOSIT_POUCH = "Deposit Money Pouch"
CLOSE = "Close"
DEPOSIT_ALL = "Deposit-All"

LOOT_INV = 773                 # LOOT
LOOT_IFACE = 1622              # TOPLEVEL_V2_LOOT
VARBIT_AREA_LOOT = 27943       # LOOT_BY_AREA
TAKE = "Take"
LOOT_ALL = "Loot All"
_ENABLED = 1
# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
DEPOSIT_BOX_REF = GamevalRef(GamevalType.INTERFACE, "BANK_DEPOSIT_BOX", DEPOSIT_BOX_IFACE)
DEPOSIT_BOX_GRID_REF = component_ref("BANK_DEPOSIT_BOX__ICONS", DEPOSIT_BOX_IFACE, DEPOSIT_BOX_GRID)
LOOT_INV_REF = GamevalRef(GamevalType.INV, "LOOT", LOOT_INV)
LOOT_REF = GamevalRef(GamevalType.INTERFACE, "TOPLEVEL_V2_LOOT", LOOT_IFACE)
AREA_LOOT_REF = GamevalRef(GamevalType.VARBIT, "LOOT_BY_AREA", VARBIT_AREA_LOOT)


def _press(game: "GameApi", iface: int, option: str) -> bool:
    if not game.snapshot().is_interface_open(iface):
        return False
    return ComponentQuery(game, iface).interact_first(option)


class DepositBox:
    def __init__(self, game: "GameApi", defs: EntityDefs | None = None) -> None:
        self._game = game
        self._defs = defs or EntityDefs()

    def is_open(self) -> bool:
        return self._game.snapshot().is_interface_open(DEPOSIT_BOX_REF.resolve())

    def open(self) -> bool:
        """"Deposit" on the nearest scene object offering it."""
        snap = self._game.snapshot()
        box = SceneObjects(snap).with_option(OPEN, self._defs).nearest(snap.self_player.tile)
        return interact(self._game, box, OPEN, self._defs)

    def close(self) -> bool:
        return _press(self._game, DEPOSIT_BOX_REF.resolve(), CLOSE)

    def deposit_all(self) -> bool:
        return _press(self._game, DEPOSIT_BOX_REF.resolve(), DEPOSIT_CARRIED)

    def deposit_worn(self) -> bool:
        return _press(self._game, DEPOSIT_BOX_REF.resolve(), DEPOSIT_WORN)

    def deposit_familiar(self) -> bool:
        return _press(self._game, DEPOSIT_BOX_REF.resolve(), DEPOSIT_FAMILIAR)

    def deposit_money_pouch(self) -> bool:
        return _press(self._game, DEPOSIT_BOX_REF.resolve(), DEPOSIT_POUCH)

    def deposit_all_except(self, *keep: "int | NameSpec", names: ItemNames | None = None) -> bool:
        """Deposit-All every backpack item that is not one of ``keep``, one click per distinct
        item, each by its slot's live "Deposit-All" op. True when every click was queued."""
        test = _keep_test(keep, names)
        return self._deposit_all_matching(lambda it: not test(it))

    def _deposit_all_matching(self, test: Callable[[InventoryItem], bool]) -> bool:
        if not self.is_open():
            return False
        queued: set[int] = set()
        all_queued = True
        for item in self._game.backpack().view().occupied():
            if test(item) and item.item_id not in queued:
                queued.add(item.item_id)
                all_queued &= self._deposit_slot(item)
        return all_queued

    def _deposit_slot(self, item: InventoryItem) -> bool:
        try:
            ops = self._game.component_ops(*DEPOSIT_BOX_GRID_REF.component(), item.slot)
        except RpcError:
            return False
        op = ops.op_named(DEPOSIT_ALL)
        if op is None:
            return False
        self._game.queue_action(component_click(*DEPOSIT_BOX_GRID_REF.component(), option=op,
                                                sub_slot=item.slot))
        return True


class LootInventory:
    def __init__(self, game: "GameApi", defs: EntityDefs | None = None) -> None:
        self._game = game
        self._defs = defs or EntityDefs()

    def items(self) -> InventoryView:
        """The loot window's inventory (773)."""
        return InventoryView(self._game.snapshot().inventory(LOOT_INV_REF.resolve()))

    def contents(self) -> list[InventoryItem]:
        return self.items().occupied()

    def is_open(self) -> bool:
        return self._game.snapshot().is_interface_open(LOOT_REF.resolve())

    def is_area_loot_enabled(self) -> bool:
        return self._game.varbit(AREA_LOOT_REF.resolve()) == _ENABLED

    def open(self) -> bool:
        """With Area Loot on, "Take" on the nearest ground item opens the window."""
        if not self.is_area_loot_enabled():
            return False
        snap = self._game.snapshot()
        nearest = GroundItems(snap).nearest(snap.self_player.tile)
        return interact(self._game, nearest, TAKE, self._defs)

    def close(self) -> bool:
        return _press(self._game, LOOT_REF.resolve(), CLOSE)

    def loot_all(self) -> bool:
        return _press(self._game, LOOT_REF.resolve(), LOOT_ALL)

    def take(self, item: "int | NameSpec", *, names: ItemNames | None = None) -> bool:
        """"Take" on the loot-window slot showing ``item`` (an id, or a name / Pattern matched
        against the loot inventory as :meth:`InventoryView.matching`)."""
        if not self.is_open():
            return False
        if not (isinstance(item, int) and not isinstance(item, bool)):
            found = self.items().first_matching(item, names=names)
            if found is None:
                return False
            item = found.item_id
        return (ComponentQuery(self._game, LOOT_REF.resolve()).with_item_id(item)
                .interact_first(TAKE))
