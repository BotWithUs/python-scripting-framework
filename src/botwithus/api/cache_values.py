"""Typed reads of the cache JSON the composition APIs need: enum entries, struct params, quests.

NXTCache's JSON writes every map with string keys (``"2794": "Backhand"``). This turns them into
int-keyed dicts, and a definition the cache cannot answer (no cache, an old bwu_host) into None
rather than an exception, so a feature built on it degrades to "unknown". Tests pass their own
:class:`CacheValues` built from fixtures (:meth:`CacheValues.from_tables`).
"""
from __future__ import annotations

from typing import Any, Callable

from ..core.native import NativeUnavailable
from .definitions import CacheUnavailable, definitions


def _int_keys(raw: Any) -> dict[int, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[int, Any] = {}
    for key, value in raw.items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def _whole_int(value: Any) -> int | None:
    """An int param value; a whole-number float counts (the Java host's Double trap)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class CacheValues:
    """Enum, struct, quest and item reads. Each is read from the cache at most once."""

    def __init__(self, json_of: Callable[[str, int], Any] | None = None,
                 item_of: Callable[[int], Any] | None = None,
                 item_params_of: Callable[[int], Any] | None = None) -> None:
        self._json_of = json_of or _cache_json
        self._item_of = item_of or _cache_item
        self._item_params_of = item_params_of or _cache_item_params
        self._memo: dict[tuple[str, int], Any] = {}

    @classmethod
    def from_tables(cls, *, enums: dict[int, dict] | None = None,
                    structs: dict[int, dict] | None = None,
                    quests: dict[int, dict] | None = None,
                    items: dict[int, Any] | None = None,
                    item_params: dict[int, dict[int, Any]] | None = None) -> "CacheValues":
        """A reader over in-memory definitions in NXTCache's JSON shape (for tests)."""
        tables = {"enum": enums or {}, "struct": structs or {}, "quest": quests or {}}
        return cls(lambda kind, i: tables[kind].get(i), lambda i: (items or {}).get(i),
                   lambda i: (item_params or {}).get(i))

    def _read(self, kind: str, def_id: int) -> Any:
        key = (kind, def_id)
        if key not in self._memo:
            try:
                self._memo[key] = self._json_of(kind, def_id)
            except (CacheUnavailable, NativeUnavailable):
                return None  # not memoised: the cache may come up later
        return self._memo[key]

    def enum_entries(self, enum_id: int) -> dict[int, Any] | None:
        """``{key: value}`` of an enum, or None when it cannot be read."""
        found = self._read("enum", enum_id)
        return None if found is None else _int_keys(found.get("entries"))

    def struct_params(self, struct_id: int) -> dict[int, Any] | None:
        found = self._read("struct", struct_id)
        return None if found is None else _int_keys(found.get("params"))

    def quest(self, quest_id: int) -> dict[str, Any] | None:
        return self._read("quest", quest_id)

    def _memo_call(self, kind: str, def_id: int, fn: Callable[[int], Any]) -> Any:
        key = (kind, def_id)
        if key not in self._memo:
            try:
                self._memo[key] = fn(def_id)
            except (CacheUnavailable, NativeUnavailable):
                return None
        return self._memo[key]

    def item(self, item_id: int) -> Any:
        """The :class:`~botwithus.api.definitions.ItemDef`, or None."""
        return self._memo_call("item", item_id, self._item_of)

    def item_params(self, item_id: int) -> dict[int, Any] | None:
        """``{param id: int or str}`` of an item, or None."""
        return self._memo_call("item_params", item_id, self._item_params_of)


def int_param(params: dict[int, Any] | None, param_id: int) -> int | None:
    return None if params is None else _whole_int(params.get(param_id))


def str_param(params: dict[int, Any] | None, param_id: int) -> str | None:
    value = None if params is None else params.get(param_id)
    return value if isinstance(value, str) else None


def _cache_json(kind: str, def_id: int) -> Any:
    return getattr(definitions, kind)(def_id)


def _cache_item(item_id: int) -> Any:
    return definitions.item(item_id)


def _cache_item_params(item_id: int) -> dict[int, Any] | None:
    from .definitions import CACHE_ITEM, PARAM_INT
    found = definitions.params(CACHE_ITEM, item_id)
    if found is None:
        return None
    return {p.param_id: (p.value if p.kind == PARAM_INT else p.text) for p in found}
