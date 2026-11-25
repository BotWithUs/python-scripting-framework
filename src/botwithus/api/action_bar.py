"""The combat action bar: which bar is active, what each slot holds, using a slot, cooldowns.

Mirrors the Java host's ``combat/ActionBar``, ``AbilityCatalog``, ``Ability``, ``ActionBarSlot``
and ``Cooldown`` (JBotWithUsV2, phase J2).

* **Slot contents** come from each slot's variables (``ACTIONBAR<bar>_INT<slot>_TYPE`` /
  ``_ID`` varbits and the ``ACTIONBAR<bar>_OBJ<slot>`` varp), read in one batch per bar. Their
  ids are resolved by those gameval names (native ABI 8), all of one bar or none, as the Java
  host does. When a name does not resolve, the bar falls back to the ids in
  :mod:`botwithus.api._action_bar_ids` (generated from a gameval dump) and says so once.
* **Abilities** come from the cache: ``COMBATV2_TYPE_TO_ENUM`` (enum 13199) maps a slot's type
  code to the per-style ability enum, which maps the slot id to the ability's struct.
* **Cooldowns** are joined through the client's own scripts, **8247** (ability struct -> the
  struct its cooldown is kept under) **then 6506** (that struct -> the START/END client-cycle
  varcs). Never by name: the name join is wrong for 63 of 172 abilities. When either script
  cannot run, the cooldown reads :data:`Cooldown.UNKNOWN`; there is deliberately no fallback.
* **Units are client cycles** (20 ms), not server ticks: the 3-tick global cooldown is 90
  cycles (3 x 30). Compare against ``SnapshotView.game_cycle``, never ``server_tick``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

import logging

from . import _action_bar_ids as ids
from .actions import ActionType, GameAction
from .cache_values import CacheValues, int_param, str_param
from .gameval import GamevalIndex, GamevalRef, GamevalType, default_index

if TYPE_CHECKING:
    from .game import GameApi

SLOTS = 14
MAX_BAR = 18
SCRIPT_COOLDOWN_STRUCT = 8247
SCRIPT_COOLDOWN_WINDOW = 6506
TYPE_TO_ENUM = 13199                 # COMBATV2_TYPE_TO_ENUM (the fallback id)
VARBIT_CURRENT_BAR = 1893            # COMBATV2_ACTIONBAR_CURRENT_BAR
VARC_GLOBAL_COOLDOWN_START = 2091    # COMBATV2_GLOBAL_COOLDOWN_START_CLIENT
VARC_GLOBAL_COOLDOWN_END = 2092      # COMBATV2_GLOBAL_COOLDOWN_END_CLIENT
TYPE_TO_ENUM_REF = GamevalRef(GamevalType.ENUM, "COMBATV2_TYPE_TO_ENUM", TYPE_TO_ENUM)
CURRENT_BAR_REF = GamevalRef(GamevalType.VARBIT, "COMBATV2_ACTIONBAR_CURRENT_BAR", VARBIT_CURRENT_BAR)
GLOBAL_COOLDOWN_START_REF = GamevalRef(GamevalType.VAR_CLIENT, "COMBATV2_GLOBAL_COOLDOWN_START_CLIENT",
                                       VARC_GLOBAL_COOLDOWN_START)
GLOBAL_COOLDOWN_END_REF = GamevalRef(GamevalType.VAR_CLIENT, "COMBATV2_GLOBAL_COOLDOWN_END_CLIENT",
                                     VARC_GLOBAL_COOLDOWN_END)
SLOT_BUTTON_PREFIX = "TOPLEVEL_V2_COMBAT_BAR__GRAPHIC_"

_log = logging.getLogger(__name__)
CLIENT_CYCLE_MS = 20

PARAM_ABILITY_ID = 2793
PARAM_NAME = 2794
PARAM_COOLDOWN = 2796
PARAM_GLOBAL_COOLDOWN = 9405
DEFAULT_GLOBAL_COOLDOWN_TICKS = 3

_OP_ACTIVATE = 1
_NO_SUB = -1
_EMPTY_TYPE = 0


@dataclass(frozen=True, slots=True)
class Ability:
    """An ability, read from its cache struct."""

    struct_id: int
    type_code: int
    ability_id: int
    name: str
    cooldown_ticks: int
    global_cooldown_ticks: int

    def is_named(self, other: str) -> bool:
        return self.name.casefold() == other.casefold()

    @classmethod
    def from_struct(cls, type_code: int, struct_id: int,
                    params: dict[int, Any] | None) -> "Ability | None":
        ability_id = int_param(params, PARAM_ABILITY_ID)
        name = str_param(params, PARAM_NAME)
        if ability_id is None or name is None:
            return None
        cooldown = int_param(params, PARAM_COOLDOWN)
        gcd = int_param(params, PARAM_GLOBAL_COOLDOWN)
        return cls(struct_id, type_code, ability_id, name, 0 if cooldown is None else cooldown,
                   DEFAULT_GLOBAL_COOLDOWN_TICKS if gcd is None else gcd)


@dataclass(frozen=True, slots=True)
class Cooldown:
    """A cooldown window in client cycles. ``is_known`` False: the scripts could not run."""

    start_cycle: int
    end_cycle: int
    is_known: bool = True

    def remaining_cycles(self, now_cycle: int) -> int:
        """Cycles left; -1 when unknown."""
        return -1 if not self.is_known else max(0, self.end_cycle - now_cycle)

    def remaining_ms(self, now_cycle: int) -> int:
        cycles = self.remaining_cycles(now_cycle)
        return -1 if cycles < 0 else cycles * CLIENT_CYCLE_MS

    def is_ready(self, now_cycle: int) -> bool:
        return self.is_known and now_cycle >= self.end_cycle


Cooldown.UNKNOWN = Cooldown(0, 0, False)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class EmptySlot:
    slot: int


@dataclass(frozen=True, slots=True)
class AbilitySlot:
    slot: int
    ability: Ability


@dataclass(frozen=True, slots=True)
class ItemSlot:
    slot: int
    item_id: int


@dataclass(frozen=True, slots=True)
class UnresolvedSlot:
    """A slot holding something the ability catalog could not name."""

    slot: int
    type_code: int
    id: int


ActionBarSlot = Union[EmptySlot, AbilitySlot, ItemSlot, UnresolvedSlot]


class AbilityCatalog:
    """Abilities by action-bar ``(type code, id)`` and by name, from the cache."""

    def __init__(self, cache: CacheValues | None = None, gamevals: GamevalIndex | None = None) -> None:
        self._cache = cache or CacheValues()
        self._gamevals = gamevals
        self._type_to_enum: dict[int, int] | None = None
        self._resolved: dict[tuple[int, int], Ability | None] = {}

    def _types(self) -> dict[int, int]:
        if self._type_to_enum is None:
            loaded = self._cache.enum_entries(TYPE_TO_ENUM_REF.resolve(self._gamevals)) or {}
            mapped = {k: v for k, v in loaded.items() if isinstance(v, int)}
            if not mapped:
                return {}  # no cache (yet): answer empty without memoising
            self._type_to_enum = mapped
        return self._type_to_enum

    def resolve(self, type_code: int, slot_id: int) -> Ability | None:
        types = self._types()
        if not types:
            return None
        key = (type_code, slot_id)
        if key not in self._resolved:
            self._resolved[key] = self._load(types, type_code, slot_id)
        return self._resolved[key]

    def by_name(self, name: str) -> Ability | None:
        for type_code, enum_id in self._types().items():
            for slot_id in (self._cache.enum_entries(enum_id) or {}):
                found = self.resolve(type_code, slot_id)
                if found is not None and found.is_named(name):
                    return found
        return None

    def _load(self, types: dict[int, int], type_code: int, slot_id: int) -> Ability | None:
        enum_id = types.get(type_code)
        if enum_id is None:
            return None
        struct_id = (self._cache.enum_entries(enum_id) or {}).get(slot_id)
        if not isinstance(struct_id, int):
            return None
        return Ability.from_struct(type_code, struct_id, self._cache.struct_params(struct_id))


class ActionBar:
    """The action bar of one game connection."""

    def __init__(self, game: "GameApi", catalog: AbilityCatalog | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._gamevals = gamevals
        self.catalog = catalog or AbilityCatalog(gamevals=gamevals)
        self._slot_vars: dict[int, tuple[tuple[int, int, int], ...]] = {}
        self._buttons: tuple[int, ...] | None = None

    def _index(self) -> GamevalIndex:
        return self._gamevals if self._gamevals is not None else default_index()

    def _vars_of(self, bar: int) -> tuple[tuple[int, int, int], ...]:
        """Bar ``bar``'s (type varbit, id varbit, item varp) per slot, by name: every one of the
        bar or the generated table, never a mix. A fallback is not kept, so it is asked again."""
        if bar in self._slot_vars:
            return self._slot_vars[bar]
        index = self._index()
        named = []
        for slot in range(1, SLOTS + 1):
            base = f"ACTIONBAR{bar}_INT{slot}"
            found = (index.id(GamevalType.VARBIT, base + "_TYPE"), index.id(GamevalType.VARBIT, base + "_ID"),
                     index.id(GamevalType.VARP, f"ACTIONBAR{bar}_OBJ{slot}"))
            if None in found:
                _warn_table(f"action bar {bar} slot variables", f"{base}_TYPE / _ID / ACTIONBAR{bar}_OBJ{slot}",
                            index)
                return ids.SLOT_VARS[bar - 1]
            named.append(found)
        self._slot_vars[bar] = tuple(named)
        return self._slot_vars[bar]

    def _slot_buttons(self) -> tuple[int, ...]:
        if self._buttons is None:
            refs = [GamevalRef(GamevalType.COMPONENT, f"{SLOT_BUTTON_PREFIX}{s}", ids.SLOT_BUTTONS[s - 1])
                    for s in range(1, SLOTS + 1)]
            resolved = tuple(ref.resolve(self._index()) for ref in refs)
            if self._index().is_available():
                self._buttons = resolved
            return resolved
        return self._buttons

    def active_bar(self) -> int:
        """The bar shown now (1..18), or -1."""
        bar = self._game.varbit(CURRENT_BAR_REF.resolve(self._gamevals))
        return bar if 1 <= bar <= MAX_BAR else -1

    def slots(self) -> list[ActionBarSlot]:
        bar = self.active_bar()
        return [] if bar < 0 else self.slots_of(bar)

    def slots_of(self, bar: int) -> list[ActionBarSlot]:
        """Slots 1..14 of ``bar``: one varbit batch and one varp batch."""
        if not 1 <= bar <= MAX_BAR:
            return []
        slot_vars = self._vars_of(bar)
        bits = self._game.read_varbits([v for t, i, _ in slot_vars for v in (t, i)])
        items = self._game.varps([o for _, _, o in slot_vars])
        if len(bits) != 2 * SLOTS or len(items) != SLOTS:
            return []
        return [self._decode(n + 1, bits[2 * n].value, bits[2 * n + 1].value, items[n])
                for n in range(SLOTS)]

    def slot(self, slot: int) -> ActionBarSlot | None:
        found = self.slots()
        return found[slot - 1] if 1 <= slot <= len(found) else None

    def find_ability(self, name: str) -> AbilitySlot | None:
        for held in self.slots():
            if isinstance(held, AbilitySlot) and held.ability.is_named(name):
                return held
        return None

    def contains_ability(self, name: str) -> bool:
        return self.find_ability(name) is not None

    def use_slot(self, slot: int) -> bool:
        """Press slot 1..14 of the bar shown (its GRAPHIC_n layer; BUTTON_n has no ops)."""
        if not 1 <= slot <= SLOTS:
            return False
        self._game.queue_action(GameAction(ActionType.COMPONENT, _OP_ACTIVATE, _NO_SUB,
                                           self._slot_buttons()[slot - 1]))
        return True

    def use_ability(self, name: str) -> bool:
        held = self.find_ability(name)
        return held is not None and self.use_slot(held.slot)

    def global_cooldown(self) -> Cooldown:
        return Cooldown(self._game.varc_int(GLOBAL_COOLDOWN_START_REF.resolve(self._gamevals)),
                        self._game.varc_int(GLOBAL_COOLDOWN_END_REF.resolve(self._gamevals)))

    def cooldown(self, ability: "Ability | str") -> Cooldown:
        """The ability's cooldown window, joined through scripts 8247 then 6506.
        :data:`Cooldown.UNKNOWN` when either script cannot run or a name is unknown."""
        if isinstance(ability, str):
            found = self.catalog.by_name(ability)
            if found is None:
                return Cooldown.UNKNOWN  # type: ignore[attr-defined]
            ability = found
        struct = self._run_script(SCRIPT_COOLDOWN_STRUCT, ability.struct_id, 1)
        if not struct or not isinstance(struct[0], int):
            return Cooldown.UNKNOWN  # type: ignore[attr-defined]
        window = self._run_script(SCRIPT_COOLDOWN_WINDOW, struct[0], 2)
        if len(window) < 2 or not all(isinstance(v, int) for v in window[:2]):
            return Cooldown.UNKNOWN  # type: ignore[attr-defined]
        return Cooldown(window[0], window[1])

    def cooldown_remaining_ms(self, ability: "Ability | str") -> int:
        """Ms left on ``ability``'s cooldown against the snapshot's game cycle; -1 unknown."""
        return self.cooldown(ability).remaining_ms(self._game.snapshot().game_cycle)

    def _decode(self, slot: int, type_code: int, slot_id: int, item_id: int) -> ActionBarSlot:
        if item_id > 0:
            return ItemSlot(slot, item_id)
        if type_code == _EMPTY_TYPE or type_code < 0:
            return EmptySlot(slot)
        found = self.catalog.resolve(type_code, slot_id)
        return AbilitySlot(slot, found) if found is not None else UnresolvedSlot(slot, type_code,
                                                                                   slot_id)

    def _run_script(self, script_id: int, arg: int, returns: int) -> list[Any]:
        """Run ``script_id(arg)`` for ``returns`` ints; [] when it cannot run. The handle is
        always released."""
        from ..core.rpc import RpcError
        try:
            handle = self._game.script_handle(script_id)
        except RpcError:
            return []
        if not handle:
            return []
        try:
            return self._game.execute_script(handle, [arg], ["int"] * returns)
        except RpcError:
            return []
        finally:
            try:
                self._game.destroy_script_handle(handle)
            except RpcError:
                pass  # a handle that will not release costs the agent a slot, not this read


_warned_tables: set[str] = set()


def _warn_table(what: str, names: str, index: GamevalIndex) -> None:
    if what in _warned_tables:
        return
    _warned_tables.add(what)
    reason = "not in the index" if index.is_available() else index.unavailable_reason()
    _log.warning("gameval %s did not resolve (%s); using the generated ids for %s", names, reason, what)
