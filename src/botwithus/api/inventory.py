"""Inventories: read-only views over the snapshot, and containers you can click.

:class:`InventoryView` is a read-only wrapper around ``SnapshotView.inventory(inv_id)``. Slots
for an inventory are contiguous; an empty slot has ``item_id == -1``.

:class:`InventoryContainer` binds an inventory to the interface grid that shows it, so a slot
can be clicked: ``game.backpack()``, ``game.bank()`` and ``game.equipment()``. Each read takes
the current snapshot, so a container never goes stale.

**Item names.** Neither the snapshot nor any agent RPC carries an item's name, so every method
that matches an item by NAME takes a ``names`` resolver, a callable from item id to its name (or
None when unknown). It defaults to :func:`botwithus.api.definitions.item_name`, which reads the
item definitions from the game cache through the native host (native ABI 7): a noted item reads
as its unnoted item, as the game shows it. Pass your own to match on something else, or on a
bwu_host without cache reads (the default then raises ``NativeUnavailable``). Option names
("Drop", "Eat") need no resolver: they are read live from the slot (``get_component_ops``).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Union

from ..core import layout
from ..core.rpc import RpcError
from .actions import BACKPACK_COMPONENT, BACKPACK_IFACE, component_click
from .snapshot_types import Inventory, InventoryItem

if TYPE_CHECKING:
    from .game import GameApi

ItemNames = Callable[[int], "str | None"]
ItemDefs = Callable[[int], Any]
# A whole item name (str, case-insensitive) or a regular expression that must match the whole
# name (re.Pattern, as the Java host's Pattern.matches()).
NameSpec = Union[str, "re.Pattern[str]"]

# The interface grid each inventory is shown in. Mirrors JBotWithUsV2 api/.../inventory/
# Bank.java (517:201, BANK__BANK_INV) and Equipment.java (1464:15). Backpack: actions.py.
BANK_IFACE = 517
BANK_COMPONENT = 201
EQUIPMENT_IFACE = 1464
EQUIPMENT_COMPONENT = 15

_OPS_NOT_FOUND = "get_component_ops: not_found"


def _name_matches(item: InventoryItem, needle: str, names: ItemNames) -> bool:
    """Case-insensitive substring of the item's name, as the Java host's ``count(String)``."""
    if item.empty:
        return False
    name = names(item.item_id)
    return bool(name) and needle.casefold() in name.casefold()


def _resolver(names: ItemNames | None) -> ItemNames:
    """``names``, or the cache's item names when it is None."""
    if names is None:
        from .definitions import item_name
        return item_name
    if not callable(names):
        raise TypeError("names= must be a callable from item id to name (or None for the "
                        "cache's item names)")
    return names


def _item_defs(item_defs: ItemDefs | None) -> ItemDefs:
    """``item_defs``, or the cache's item definitions (None when the cache cannot answer)."""
    if item_defs is not None:
        return item_defs
    from ..core.native import NativeUnavailable
    from .definitions import CacheUnavailable, definitions

    def lookup(item_id: int) -> Any:
        try:
            return definitions.item(item_id)
        except (CacheUnavailable, NativeUnavailable):
            return None
    return lookup


def _spec_matcher(spec: NameSpec) -> Callable[[str], bool]:
    """Whole-name matching: a str equal ignoring case, a Pattern that matches the whole name."""
    if isinstance(spec, re.Pattern):
        return lambda name: spec.fullmatch(name) is not None
    wanted = _needle(spec).casefold()
    return lambda name: name.strip().casefold() == wanted


def _by_names(specs: Iterable[NameSpec], names: ItemNames) -> Callable[[InventoryItem], bool]:
    matchers = [_spec_matcher(spec) for spec in specs]

    def test(item: InventoryItem) -> bool:
        if item.empty:
            return False
        name = names(item.item_id)
        return bool(name) and any(match(name) for match in matchers)
    return test


def _needle(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"an item name must be a non-empty string, got {name!r}")
    return name.strip()


class InventoryView:
    """A queried single inventory. ``None``-safe: absent inventory reads as empty."""

    def __init__(self, inv: Inventory | None) -> None:
        self._inv = inv

    @property
    def present(self) -> bool:
        """Whether the agent published this inventory in this frame. The bank is published
        only while it is open, so an absent bank is not an empty one."""
        return self._inv is not None

    @property
    def items(self) -> tuple[InventoryItem, ...]:
        return self._inv.items if self._inv else ()

    def occupied(self) -> list[InventoryItem]:
        return [it for it in self.items if not it.empty]

    def contains(self, item_id: int) -> bool:
        return any(it.item_id == item_id for it in self.items)

    def count(self, item_id: int) -> int:
        """Total quantity of ``item_id`` across all slots (sums stacks)."""
        return sum(it.quantity for it in self.items if it.item_id == item_id)

    def free_slots(self) -> int:
        return sum(1 for it in self.items if it.empty)

    def is_empty(self) -> bool:
        return not self.occupied()

    def is_full(self) -> bool:
        return self.free_slots() == 0 and bool(self.items)

    def first(self, item_id: int) -> InventoryItem | None:
        for it in self.items:
            if it.item_id == item_id:
                return it
        return None

    def slot(self, slot: int) -> InventoryItem | None:
        """The 0-based ``slot``, or None when it is out of range."""
        items = self.items
        return items[slot] if 0 <= slot < len(items) else None

    # --- by item name (needs a resolver; see the module docstring) ------------

    def all_named(self, name: str, *, names: ItemNames | None = None) -> list[InventoryItem]:
        """Every occupied slot whose item name contains ``name``, case-insensitively."""
        resolve = _resolver(names)
        needle = _needle(name)
        return [it for it in self.items if _name_matches(it, needle, resolve)]

    def first_named(self, name: str, *, names: ItemNames | None = None) -> InventoryItem | None:
        found = self.all_named(name, names=names)
        return found[0] if found else None

    def contains_named(self, name: str, *, names: ItemNames | None = None) -> bool:
        return self.first_named(name, names=names) is not None

    def count_named(self, name: str, *, names: ItemNames | None = None) -> int:
        """Total quantity across the slots :meth:`all_named` matches."""
        return sum(it.quantity for it in self.all_named(name, names=names))

    # --- whole names and patterns (J1) ------------------------------------------------
    #
    # Unlike the *_named reads above (a SUBSTRING, as the Java host's count(String)), these
    # match the WHOLE name: they decide what to click or deposit, and "Logs" must not match
    # "Oak logs". A str matches ignoring case; a compiled re.Pattern must match the whole name.

    def matching(self, *specs: NameSpec, names: ItemNames | None = None) -> list[InventoryItem]:
        """Occupied slots whose name matches any of ``specs`` (whole name or Pattern)."""
        test = _by_names(specs, _resolver(names))
        return [it for it in self.items if test(it)]

    def first_matching(self, *specs: NameSpec,
                       names: ItemNames | None = None) -> InventoryItem | None:
        found = self.matching(*specs, names=names)
        return found[0] if found else None

    def contains_matching(self, *specs: NameSpec, names: ItemNames | None = None) -> bool:
        return self.first_matching(*specs, names=names) is not None

    def count_matching(self, *specs: NameSpec, names: ItemNames | None = None) -> int:
        return sum(it.quantity for it in self.matching(*specs, names=names))

    def first_exact(self, name: str, *, names: ItemNames | None = None) -> InventoryItem | None:
        return self.first_matching(name, names=names)

    def contains_all_of(self, *item_names: str, names: ItemNames | None = None) -> bool:
        resolve = _resolver(names)
        return all(self.contains_matching(n, names=resolve) for n in item_names)

    def contains_any_of(self, *item_names: str, names: ItemNames | None = None) -> bool:
        return self.contains_matching(*item_names, names=names)

    def contains_any_except(self, *keep: int | NameSpec, names: ItemNames | None = None) -> bool:
        """Whether any occupied slot holds an item that is not one of ``keep`` (item ids, or
        names / Patterns as :meth:`matching`)."""
        test = _keep_test(keep, names)
        return any(not test(it) for it in self.occupied())

    def all_in_category(self, category: int, *,
                        item_defs: ItemDefs | None = None) -> list[InventoryItem]:
        """Occupied slots whose item's cache category is ``category``."""
        lookup = _item_defs(item_defs)
        return [it for it in self.occupied()
                if (d := lookup(it.item_id)) is not None and d.category == category]

    def contains_category(self, category: int, *, item_defs: ItemDefs | None = None) -> bool:
        return bool(self.all_in_category(category, item_defs=item_defs))

    def items_with_option(self, option: str, *,
                          item_defs: ItemDefs | None = None) -> list[InventoryItem]:
        """Occupied slots whose item lists ``option`` among its inventory ops (cache)."""
        lookup = _item_defs(item_defs)
        return [it for it in self.occupied()
                if (d := lookup(it.item_id)) is not None and d.has_inventory_op(option)]


def _keep_test(keep: Iterable[int | NameSpec],
               names: ItemNames | None) -> Callable[[InventoryItem], bool]:
    keep = list(keep)
    ids = {k for k in keep if isinstance(k, int) and not isinstance(k, bool)}
    specs = [k for k in keep if isinstance(k, (str, re.Pattern))]
    by_name = _by_names(specs, _resolver(names)) if specs else (lambda it: False)
    return lambda it: it.item_id in ids or by_name(it)


class InventoryContainer:
    """An inventory you can click: reads from the current snapshot, clicks through the
    COMPONENT action on its slot grid (``param2`` = the slot).

    Every ``interact*`` returns True when one action was queued and False, with nothing sent,
    when there is nothing to click: the inventory is not published, the slot is out of range,
    the item is not there, or (for an option given by name) the grid is not open or the slot
    lists no op of that name. Errors from queueing the action raise, as
    :meth:`GameApi.queue_action` does.
    """

    def __init__(self, game: "GameApi", inv_id: int, iface: int, comp: int) -> None:
        self._game = game
        self.inv_id = inv_id
        self.iface = iface
        self.comp = comp

    def __repr__(self) -> str:
        return f"InventoryContainer(inv {self.inv_id} at {self.iface}:{self.comp})"

    def view(self) -> InventoryView:
        """A read-only view of the inventory in the current snapshot."""
        return InventoryView(self._game.snapshot().inventory(self.inv_id))

    # --- reads (each takes a fresh snapshot) ----------------------------------

    def items(self) -> list[InventoryItem]:
        """The occupied slots."""
        return self.view().occupied()

    def contains(self, item_id: int) -> bool:
        return self.view().contains(item_id)

    def count(self, item_id: int) -> int:
        return self.view().count(item_id)

    def contains_named(self, name: str, *, names: ItemNames | None = None) -> bool:
        return self.view().contains_named(name, names=names)

    def count_named(self, name: str, *, names: ItemNames | None = None) -> int:
        return self.view().count_named(name, names=names)

    def free_slots(self) -> int:
        return self.view().free_slots()

    def is_full(self) -> bool:
        return self.view().is_full()

    def obj_vars(self, slot: int) -> dict[int, int]:
        """``{var_id: value}`` of the item in ``slot`` (one ``get_obj_vars`` round trip)."""
        return self._game.slot_obj_vars(self.inv_id, slot)

    # --- clicks ---------------------------------------------------------------

    def interact(self, slot: int, option: int | str) -> bool:
        """Click 0-based ``slot`` with ``option``: a 1-based op number, or the name the menu
        shows ("Drop"), matched against the slot's live ops (one extra round trip)."""
        view = self.view()
        if not view.present or view.slot(slot) is None:
            return False
        if isinstance(option, str):
            op = self._op_named(slot, option)
            if op is None:
                return False
        elif isinstance(option, bool) or not isinstance(option, int) or option < 1:
            raise ValueError(f"option must be a 1-based op number or a name, got {option!r}")
        else:
            op = option
        self._game.queue_action(component_click(self.iface, self.comp, option=op, sub_slot=slot))
        return True

    def interact_item(self, item_id: int, option: int | str) -> bool:
        """Click the first slot holding ``item_id``."""
        found = self.view().first(item_id)
        return found is not None and self.interact(found.slot, option)

    def interact_named(self, item_name: str, option: int | str, *,
                       names: ItemNames | None = None) -> bool:
        """Click the first slot whose item name contains ``item_name``
        (see :meth:`InventoryView.all_named`)."""
        found = self.view().first_named(item_name, names=names)
        return found is not None and self.interact(found.slot, option)

    # --- whole names and patterns (J1; see InventoryView.matching) -------------------

    def matching(self, *specs: NameSpec, names: ItemNames | None = None) -> list[InventoryItem]:
        return self.view().matching(*specs, names=names)

    def first_matching(self, *specs: NameSpec,
                       names: ItemNames | None = None) -> InventoryItem | None:
        return self.view().first_matching(*specs, names=names)

    def contains_matching(self, *specs: NameSpec, names: ItemNames | None = None) -> bool:
        return self.view().contains_matching(*specs, names=names)

    def count_matching(self, *specs: NameSpec, names: ItemNames | None = None) -> int:
        return self.view().count_matching(*specs, names=names)

    def first_exact(self, name: str, *, names: ItemNames | None = None) -> InventoryItem | None:
        return self.view().first_exact(name, names=names)

    def contains_all_of(self, *item_names: str, names: ItemNames | None = None) -> bool:
        return self.view().contains_all_of(*item_names, names=names)

    def contains_any_of(self, *item_names: str, names: ItemNames | None = None) -> bool:
        return self.view().contains_any_of(*item_names, names=names)

    def contains_any_except(self, *keep: int | NameSpec, names: ItemNames | None = None) -> bool:
        return self.view().contains_any_except(*keep, names=names)

    def all_in_category(self, category: int, *,
                        item_defs: ItemDefs | None = None) -> list[InventoryItem]:
        return self.view().all_in_category(category, item_defs=item_defs)

    def contains_category(self, category: int, *, item_defs: ItemDefs | None = None) -> bool:
        return self.view().contains_category(category, item_defs=item_defs)

    def items_with_option(self, option: str, *,
                          item_defs: ItemDefs | None = None) -> list[InventoryItem]:
        return self.view().items_with_option(option, item_defs=item_defs)

    def interact_first(self, spec: NameSpec, option: int | str, *,
                       names: ItemNames | None = None) -> bool:
        """Click the first slot whose WHOLE name matches ``spec`` (a str ignoring case, or a
        Pattern). False, with nothing sent, when no slot matches or it lists no such option."""
        found = self.first_matching(spec, names=names)
        return found is not None and self.interact(found.slot, option)

    def _op_named(self, slot: int, option: str) -> int | None:
        try:
            ops = self._game.component_ops(self.iface, self.comp, slot)
        except RpcError as exc:
            if exc.agent_message == _OPS_NOT_FOUND:
                return None
            raise
        return ops.op_named(option)


def backpack(snapshot) -> InventoryView:
    return InventoryView(snapshot.inventory(layout.INV_BACKPACK))


def bank(snapshot) -> InventoryView:
    """The bank (inventory 95).

    The agent publishes the bank only while the bank interface is open. While it is
    closed this returns an empty view, so ``bank(s).is_empty()`` does not mean the bank
    has nothing in it. Check ``present`` (or that the bank is open) before trusting an empty
    result.
    """
    return InventoryView(snapshot.inventory(layout.INV_BANK))


def equipment(snapshot) -> InventoryView:
    """Worn equipment (inventory 94). Published whether or not the bank is open."""
    return InventoryView(snapshot.inventory(layout.INV_EQUIPMENT))


USE = "Use"


class Backpack(InventoryContainer):
    """The backpack (inv 93), clicked through 1473:5."""

    def __init__(self, game: "GameApi") -> None:
        super().__init__(game, layout.INV_BACKPACK, BACKPACK_IFACE, BACKPACK_COMPONENT)

    def use(self, item: "int | NameSpec", *, names: ItemNames | None = None) -> bool:
        """Select the item for "Use": by item id, whole name or Pattern."""
        if isinstance(item, int) and not isinstance(item, bool):
            return self.interact_item(item, USE)
        return self.interact_first(item, USE, names=names)


def backpack_container(game: "GameApi") -> Backpack:
    """The backpack (inv 93), clicked through 1473:5."""
    return Backpack(game)


def bank_container(game: "GameApi") -> InventoryContainer:
    """The bank (inv 95), clicked through the banked-items grid 517:201. Published, and
    clickable, only while the bank is open. A :class:`botwithus.api.banking.Bank`."""
    from .banking import Bank
    return Bank(game)


def equipment_container(game: "GameApi") -> InventoryContainer:
    """Worn equipment (inv 94), clicked through 1464:15. Slot indices are the game's
    equipment-inventory indices, with gaps."""
    return InventoryContainer(game, layout.INV_EQUIPMENT, EQUIPMENT_IFACE, EQUIPMENT_COMPONENT)
