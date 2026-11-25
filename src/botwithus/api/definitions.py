"""Cache definitions: items, NPCs, locs and the long tail, read through the native host.

The native host links NXTCache.dll (the same library and cache the walker's varbit gating
uses) and exposes it as ``_native.cache_*`` (native ABI 7). Definitions are process-wide: no
client is needed, and each one is read from the cache once and kept by the host.

Typed where scripts use them most -- :meth:`Definitions.item`, :meth:`npc`, :meth:`loc` and
:meth:`params` -- and the cache's own JSON for everything else (:meth:`json`, :meth:`enum`,
:meth:`struct`, :meth:`param`, :meth:`varbit`, :meth:`inv`, :meth:`quest`). The item fixups
match the Java host: ground op 3 reads "Take" when the cache leaves it unset, and a noted item
with no name of its own takes its unnoted item's name and members flag.

A missing id is ``None``. No cache, or an NXTCache.dll older than these reads, raises
:class:`CacheUnavailable`; a bwu_host older than ABI 7 raises
:class:`~botwithus.core.native.NativeUnavailable`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..core import native

# bwu_host_surface.h BWU_CACHE_*: the type ids cache_json / cache_params take.
CACHE_ITEM = 0
CACHE_NPC = 1
CACHE_LOC = 2
CACHE_ENUM = 3
CACHE_STRUCT = 4
CACHE_PARAM = 5
CACHE_VARBIT = 6
CACHE_INV = 7
CACHE_QUEST = 8
CACHE_WORLDMAP = 9

PARAM_INT = 0
PARAM_STRING = 1

_ITEM_NAME_CACHE = 4096   # item names kept on this side, for by-name matching in loops


class CacheUnavailable(RuntimeError):
    """The cache cannot be read: NXTCache.dll is missing, the cache could not be opened, or the
    NXTCache.dll predates the read asked for. The message is the host's reason."""


@dataclass(frozen=True, slots=True)
class Param:
    """One param of a definition: an int (``value``) or a string (``text``)."""

    param_id: int
    kind: int               # PARAM_INT | PARAM_STRING
    value: int | None
    text: str | None
    text_truncated: bool    # the string was cut to fit; cache_json has all of it

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "Param":
        return cls(int(d["param_id"]), int(d["kind"]), d["value"], d["text"], bool(d["text_truncated"]))


@dataclass(frozen=True, slots=True)
class ItemDef:
    id: int
    name: str
    members: bool
    stackable: bool
    noted: bool              # this id is the noted form
    noted_id: int            # the other form (note <-> item); -1 none
    category: int            # 0 when the item sets none
    shop_price: int
    ge_buy_limit: int
    exchangeable: bool       # tradeable on the Grand Exchange
    wearpos: int             # -1 unset
    wearpos2: int
    wearpos3: int
    param_count: int
    inventory_ops: tuple[str, ...]   # op 1..5; "" for an empty slot
    ground_ops: tuple[str, ...]      # op 3 is "Take" when the cache leaves it unset
    text_truncated: int              # BWU_CACHE_CUT_* bits

    def has_inventory_op(self, op: str) -> bool:
        return any(o.casefold() == op.casefold() for o in self.inventory_ops if o)

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "ItemDef":
        return cls(int(d["id"]), str(d["name"]), bool(d["members"]), bool(d["stackable"]),
                   bool(d["noted"]), int(d["noted_id"]), int(d["category"]), int(d["shop_price"]),
                   int(d["ge_buy_limit"]), bool(d["exchangeable"]), int(d["wearpos"]),
                   int(d["wearpos2"]), int(d["wearpos3"]), int(d["param_count"]),
                   tuple(d["inventory_ops"]), tuple(d["ground_ops"]), int(d["text_truncated"]))


@dataclass(frozen=True, slots=True)
class NpcDef:
    id: int
    name: str
    combat_level: int
    visible: bool
    clickable: bool
    varbit_id: int           # -1: does not transform on a varbit
    varp_id: int             # -1: does not transform on a varp
    transform_count: int
    param_count: int
    ops: tuple[str, ...]
    text_truncated: int

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "NpcDef":
        return cls(int(d["id"]), str(d["name"]), int(d["combat_level"]), bool(d["visible"]),
                   bool(d["clickable"]), int(d["varbit_id"]), int(d["varp_id"]),
                   int(d["transform_count"]), int(d["param_count"]), tuple(d["ops"]),
                   int(d["text_truncated"]))


@dataclass(frozen=True, slots=True)
class LocDef:
    id: int
    name: str
    size_x: int
    size_y: int
    interact_type: int
    solid_type: int
    members: bool
    varbit_id: int
    varp_id: int
    transform_count: int
    map_sprite_id: int       # -1 none
    param_count: int
    ops: tuple[str, ...]
    text_truncated: int

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "LocDef":
        return cls(int(d["id"]), str(d["name"]), int(d["size_x"]), int(d["size_y"]),
                   int(d["interact_type"]), int(d["solid_type"]), bool(d["members"]),
                   int(d["varbit_id"]), int(d["varp_id"]), int(d["transform_count"]),
                   int(d["map_sprite_id"]), int(d["param_count"]), tuple(d["ops"]),
                   int(d["text_truncated"]))


def _fn(name: str) -> Any:
    fn = getattr(native.backend(), name, None)
    if fn is None:
        raise native.NativeUnavailable(f"this bwu_host has no {name} (cache definitions); it"
                                       " predates them (native ABI 7) -- update it")
    return fn


def _call(name: str, *args: Any) -> Any:
    try:
        return _fn(name)(*args)
    except RuntimeError as exc:
        if isinstance(exc, native.NativeUnavailable):
            raise
        raise CacheUnavailable(str(exc)) from exc


class Definitions:
    """Cache definitions. Stateless: every instance reads the same process-wide cache."""

    def available(self) -> bool:
        """True when the cache opens. False (not an exception) when it cannot, or on a
        bwu_host without cache reads."""
        try:
            return _call("cache_status") == 1
        except (CacheUnavailable, native.NativeUnavailable):
            return False

    def item(self, item_id: int) -> ItemDef | None:
        d = _call("cache_item", int(item_id))
        return None if d is None else ItemDef.from_native(d)

    def item_name(self, item_id: int) -> str | None:
        """The item's name as the game shows it (a note reads as its item), or None for an
        empty slot, an unknown id, or a nameless item. Kept on this side too: by-name matching
        asks for the same ids every loop."""
        return _item_name(int(item_id))

    def npc(self, npc_id: int) -> NpcDef | None:
        d = _call("cache_npc", int(npc_id))
        return None if d is None else NpcDef.from_native(d)

    def loc(self, loc_id: int) -> LocDef | None:
        d = _call("cache_loc", int(loc_id))
        return None if d is None else LocDef.from_native(d)

    def params(self, cache_type: int, def_id: int) -> list[Param] | None:
        """The params of an item, npc, loc, struct or quest, ascending param id."""
        raw = _call("cache_params", int(cache_type), int(def_id))
        return None if raw is None else [Param.from_native(p) for p in raw]

    def int_param(self, cache_type: int, def_id: int, param_id: int) -> int | None:
        """One int param, or None when the definition does not set it."""
        for p in self.params(cache_type, def_id) or ():
            if p.param_id == param_id and p.kind == PARAM_INT:
                return p.value
        return None

    def json(self, cache_type: int, def_id: int) -> dict[str, Any] | None:
        """The whole definition as NXTCache's JSON, parsed; None for a missing id."""
        text = _call("cache_json", int(cache_type), int(def_id))
        return None if text is None else json.loads(text)

    def enum(self, enum_id: int) -> dict[str, Any] | None:
        return self.json(CACHE_ENUM, enum_id)

    def struct(self, struct_id: int) -> dict[str, Any] | None:
        return self.json(CACHE_STRUCT, struct_id)

    def param(self, param_id: int) -> dict[str, Any] | None:
        return self.json(CACHE_PARAM, param_id)

    def varbit(self, varbit_id: int) -> dict[str, Any] | None:
        return self.json(CACHE_VARBIT, varbit_id)

    def inv(self, inv_id: int) -> dict[str, Any] | None:
        return self.json(CACHE_INV, inv_id)

    def quest(self, quest_id: int) -> dict[str, Any] | None:
        """A quest definition. Some ids are stub records with an empty ``listName``; prefer
        the ones that have one."""
        return self.json(CACHE_QUEST, quest_id)


@lru_cache(maxsize=_ITEM_NAME_CACHE)
def _item_name(item_id: int) -> str | None:
    if item_id < 0:
        return None
    d = _call("cache_item", item_id)
    return (d["name"] or None) if d is not None else None


definitions = Definitions()


def item_name(item_id: int) -> str | None:
    """The default ``names=`` resolver of the inventory APIs: the item's name from the cache."""
    return definitions.item_name(item_id)


def clear_item_names() -> None:
    """Forget the item names kept on this side (they only change with a game update)."""
    _item_name.cache_clear()
