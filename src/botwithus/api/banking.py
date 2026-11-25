"""The bank: open it, deposit and withdraw by id, whole name or Pattern, presets, modes.

Mirrors the Java host's ``inventory/Bank`` (JBotWithUsV2, phase J1). Every click goes through
the action queue. Each method that needs the bank open answers False, with nothing sent, while
it is closed.

Names match the WHOLE item name, ignoring case, and a compiled ``re.Pattern`` must match the
whole name (:meth:`InventoryView.matching`): these decide what gets deposited, and with a
substring "Logs" would deposit Oak logs.

The ids are the ones the Java host resolves from its gameval names (``BANK__BANK_INV`` ...);
the native host exposes no gameval index, so they are fixed here, named after the gameval.
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Callable

from ..core import layout
from .actions import component_click
from .entities import Npcs, SceneObjects
from .gameval import GamevalIndex, GamevalRef, GamevalType, component_ref
from .interact import EntityDefs, interact
from .inventory import (BANK_COMPONENT, BANK_IFACE, InventoryContainer, ItemNames, NameSpec,
                        _keep_test)
from .snapshot_types import InventoryItem

if TYPE_CHECKING:
    from .game import GameApi

BACKPACK_COMPONENT = 15          # BANK__INVENTORY_CLICK: the backpack grid inside the bank
CLOSE_BUTTON = 317               # BANK__CLOSE_BUTTON_LAYER
DEPOSIT_BACKPACK_BUTTON = 39     # BANK__BANK_INV_BUTTON
DEPOSIT_WORN_BUTTON = 42         # BANK__BANK_WORN_BUTTON
DEPOSIT_FAMILIAR_BUTTON = 45     # BANK__BANK_BOB_BUTTON
DEPOSIT_POUCH_BUTTON = 48        # BANK__BANK_POUCH_BUTTON
PRESET_BUTTONS = 119             # BANK__SHARE_QUICK_CLICK
NOTE_MODE_BUTTON = 127           # BANK__BANK_CERT_BUTTON
MODE_BUTTONS = {"ONE": 93, "FIVE": 96, "TEN": 99, "ALL": 103, "CUSTOM": 106}  # BANK__DEFAULT_OP_*

VARBIT_TRANSFER_MODE = 45189     # BANK_CUSTOMOP
VARBIT_PRESET_PAGE = 49662       # SHARE_PRESET_BANK_BUTTON_PAGE
VARP_WITHDRAW_AS_NOTE = 160      # BANKCERT

# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
BANK_REF = GamevalRef(GamevalType.INTERFACE, "BANK", BANK_IFACE)
BANK_ITEMS_REF = component_ref("BANK__BANK_INV", BANK_IFACE, BANK_COMPONENT)
TRANSFER_MODE_REF = GamevalRef(GamevalType.VARBIT, "BANK_CUSTOMOP", VARBIT_TRANSFER_MODE)
PRESET_PAGE_REF = GamevalRef(GamevalType.VARBIT, "SHARE_PRESET_BANK_BUTTON_PAGE", VARBIT_PRESET_PAGE)
WITHDRAW_AS_NOTE_REF = GamevalRef(GamevalType.VARP, "BANKCERT", VARP_WITHDRAW_AS_NOTE)
# Every bank component this module clicks, by its fallback component number.
_COMPONENT_NAMES = {
    BACKPACK_COMPONENT: "BANK__INVENTORY_CLICK", BANK_COMPONENT: "BANK__BANK_INV",
    CLOSE_BUTTON: "BANK__CLOSE_BUTTON_LAYER", DEPOSIT_BACKPACK_BUTTON: "BANK__BANK_INV_BUTTON",
    DEPOSIT_WORN_BUTTON: "BANK__BANK_WORN_BUTTON", DEPOSIT_FAMILIAR_BUTTON: "BANK__BANK_BOB_BUTTON",
    DEPOSIT_POUCH_BUTTON: "BANK__BANK_POUCH_BUTTON", PRESET_BUTTONS: "BANK__SHARE_QUICK_CLICK",
    NOTE_MODE_BUTTON: "BANK__BANK_CERT_BUTTON", MODE_BUTTONS["ONE"]: "BANK__DEFAULT_OP_1",
    MODE_BUTTONS["FIVE"]: "BANK__DEFAULT_OP_5", MODE_BUTTONS["TEN"]: "BANK__DEFAULT_OP_10",
    MODE_BUTTONS["ALL"]: "BANK__DEFAULT_OP_ALL_BUTTON", MODE_BUTTONS["CUSTOM"]: "BANK__DEFAULT_OP_X",
}
COMPONENT_REFS = {comp: component_ref(name, BANK_IFACE, comp) for comp, name in _COMPONENT_NAMES.items()}
WITHDRAW_AS_NOTE = 1

OPEN_RADIUS = 10
BANK_OPTION = "Bank"
USE_OPTION = "Use"
LOAD_LAST_PRESET_OPTION = "Load Last Preset from"

_OP_CLICK = 1
_OP_ENTER_AMOUNT = 6
_NO_SUB = -1
_MAX_PRESET = 18
_PRESETS_PER_PAGE = 9
_PRESET_PAGE_SWITCH_SUB = 100


class TransferAmount(enum.Enum):
    """A transfer amount: ``(varbit value, menu op)``."""

    ONE = (2, 2)
    FIVE = (3, 3)
    TEN = (4, 4)
    ALL = (7, 7)
    CUSTOM = (5, 5)

    @property
    def varbit_value(self) -> int:
        return self.value[0]

    @property
    def menu_op(self) -> int:
        return self.value[1]


class WithdrawMode(enum.Enum):
    ITEM = "item"
    NOTE = "note"


class Bank(InventoryContainer):
    """The bank (inv 95, grid 517:201) and its buttons."""

    def __init__(self, game: "GameApi", defs: EntityDefs | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        super().__init__(game, layout.INV_BANK, *BANK_ITEMS_REF.component(gamevals))
        self._defs = defs or EntityDefs()
        self._gamevals = gamevals

    # --- state ------------------------------------------------------------------------

    def is_open(self) -> bool:
        return self._game.snapshot().is_interface_open(BANK_REF.resolve(self._gamevals))

    def withdraw_mode(self) -> WithdrawMode:
        value = self._game.varp(WITHDRAW_AS_NOTE_REF.resolve(self._gamevals))
        return WithdrawMode.NOTE if value == WITHDRAW_AS_NOTE else WithdrawMode.ITEM

    def transfer_mode(self) -> TransferAmount:
        value = self._game.varbit(TRANSFER_MODE_REF.resolve(self._gamevals))
        return next((a for a in TransferAmount if a.varbit_value == value), TransferAmount.ONE)

    def preset_page(self) -> int:
        return self._game.varbit(PRESET_PAGE_REF.resolve(self._gamevals))

    # --- open / close -----------------------------------------------------------------

    def open(self) -> bool:
        """True when open already. Otherwise clicks the nearest booth or chest within
        :data:`OPEN_RADIUS` tiles offering "Bank" (else "Use"); with none, the nearest banker
        NPC offering "Bank". False, with nothing sent, when none is in range."""
        if self.is_open():
            return True
        snap = self._game.snapshot()
        me = snap.self_player.tile
        booth = (SceneObjects(snap).within(me, OPEN_RADIUS)
                 .where(lambda o: self._defs.has_option(o, BANK_OPTION)
                        or self._defs.has_option(o, USE_OPTION))
                 .nearest(me))
        if booth is not None:
            return (interact(self._game, booth, BANK_OPTION, self._defs)
                    or interact(self._game, booth, USE_OPTION, self._defs))
        banker = (Npcs(snap).within(me, OPEN_RADIUS)
                  .with_option(BANK_OPTION, self._defs).nearest(me))
        return interact(self._game, banker, BANK_OPTION, self._defs)

    def close(self) -> bool:
        return True if not self.is_open() else self._button(CLOSE_BUTTON)

    # --- deposits ---------------------------------------------------------------------

    def deposit_backpack(self) -> bool:
        """The "deposit backpack" button (the Java host's no-argument ``depositAll()``)."""
        return self._button_when_open(DEPOSIT_BACKPACK_BUTTON)

    def deposit_equipment(self) -> bool:
        return self._button_when_open(DEPOSIT_WORN_BUTTON)

    def deposit_familiar(self) -> bool:
        return self._button_when_open(DEPOSIT_FAMILIAR_BUTTON)

    def deposit_coins(self) -> bool:
        return self._button_when_open(DEPOSIT_POUCH_BUTTON)

    def deposit(self, item: "int | NameSpec", amount: TransferAmount = TransferAmount.ALL, *,
                names: ItemNames | None = None) -> bool:
        """Deposit the first backpack slot holding ``item`` (an id, whole name or Pattern)."""
        if not self.is_open():
            return False
        found = self._backpack_first(item, names)
        return self._slot(BACKPACK_COMPONENT, found, amount.menu_op)

    def deposit_all(self, *items: "int | NameSpec", names: ItemNames | None = None) -> bool:
        """Deposit-All every backpack item that is one of ``items`` (ids, whole names or
        Patterns), one click per distinct item. With no arguments, the deposit-backpack
        button."""
        if not items:
            return self.deposit_backpack()
        return self._deposit_all_matching(_keep_test(items, names))

    def deposit_all_except(self, *keep: "int | NameSpec", names: ItemNames | None = None) -> bool:
        """Deposit-All every backpack item that is NOT one of ``keep``, one click per distinct
        item."""
        test = _keep_test(keep, names)
        return self._deposit_all_matching(lambda it: not test(it))

    # --- withdraws --------------------------------------------------------------------

    def withdraw(self, item: "int | NameSpec", amount: TransferAmount = TransferAmount.ONE, *,
                 names: ItemNames | None = None) -> bool:
        """Withdraw the first bank slot holding ``item`` (an id, whole name or Pattern)."""
        if not self.is_open():
            return False
        if isinstance(item, int) and not isinstance(item, bool):
            found = self.view().first(item)
        else:
            found = self.view().first_matching(item, names=names)
        return self._slot(BANK_COMPONENT, found, amount.menu_op)

    def withdraw_all(self, item: "int | NameSpec", *, names: ItemNames | None = None) -> bool:
        return self.withdraw(item, TransferAmount.ALL, names=names)

    # --- presets and modes --------------------------------------------------------------

    def withdraw_preset(self, number: int) -> bool:
        """Load preset ``number`` (1..18) from the open bank, switching page first if needed."""
        if not self.is_open() or not 1 <= number <= _MAX_PRESET:
            return False
        page = 1 if number > _PRESETS_PER_PAGE else 0
        if self.preset_page() != page:
            self._button(PRESET_BUTTONS, _PRESET_PAGE_SWITCH_SUB)
        return self._button(PRESET_BUTTONS, (number - 1) % _PRESETS_PER_PAGE + 1)

    def load_last_preset(self) -> bool:
        """"Load Last Preset from" on the nearest booth or banker that offers it (the nearer of
        the two). Needs no open bank. The option text is the Java host's (from xapi)."""
        snap = self._game.snapshot()
        me = snap.self_player.tile
        booth = SceneObjects(snap).with_option(LOAD_LAST_PRESET_OPTION, self._defs).nearest(me)
        banker = Npcs(snap).with_option(LOAD_LAST_PRESET_OPTION, self._defs).nearest(me)
        if booth is not None and (banker is None
                                  or _distance(booth, me) <= _distance(banker, me)):
            return interact(self._game, booth, LOAD_LAST_PRESET_OPTION, self._defs)
        return interact(self._game, banker, LOAD_LAST_PRESET_OPTION, self._defs)

    def set_withdraw_mode(self, mode: WithdrawMode) -> bool:
        """Toggle the note button only when the mode differs. True when already in ``mode``."""
        if not self.is_open():
            return False
        if self.withdraw_mode() is mode:
            return True
        return self._button(NOTE_MODE_BUTTON)

    def set_transfer_mode(self, mode: TransferAmount) -> bool:
        if not self.is_open():
            return False
        return self._button(MODE_BUTTONS[mode.name])

    # --- helpers ----------------------------------------------------------------------

    def _backpack_first(self, item: "int | NameSpec",
                        names: ItemNames | None) -> InventoryItem | None:
        view = self._game.backpack().view()
        if isinstance(item, int) and not isinstance(item, bool):
            return view.first(item)
        return view.first_matching(item, names=names)

    def _deposit_all_matching(self, test: Callable[[InventoryItem], bool]) -> bool:
        if not self.is_open():
            return False
        queued: set[int] = set()
        for item in self._game.backpack().view().occupied():
            if test(item) and item.item_id not in queued:
                queued.add(item.item_id)
                self._slot(BACKPACK_COMPONENT, item, TransferAmount.ALL.menu_op)
        return True

    def _slot(self, comp: int, item: InventoryItem | None, op: int) -> bool:
        if item is None:
            return False
        iface, resolved = self._component(comp)
        self._game.queue_action(component_click(iface, resolved, option=op, sub_slot=item.slot))
        return True

    def _button(self, comp: int, sub: int = _NO_SUB) -> bool:
        iface, resolved = self._component(comp)
        self._game.queue_action(component_click(iface, resolved, option=_OP_CLICK, sub_slot=sub))
        return True

    def _component(self, comp: int) -> tuple[int, int]:
        """(interface, component) of the bank component whose fallback number is ``comp``, by
        its gameval name."""
        ref = COMPONENT_REFS.get(comp)
        return ref.component(self._gamevals) if ref is not None else (BANK_IFACE, comp)

    def _button_when_open(self, comp: int) -> bool:
        return self.is_open() and self._button(comp)


def _distance(entity: object, origin: object) -> int:
    from .entities import chebyshev
    return chebyshev(entity.tile, origin)  # type: ignore[attr-defined]


__all__ = ["Bank", "TransferAmount", "WithdrawMode"]
