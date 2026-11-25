"""Gameval names: the stable names the game ships for its ids, resolved by type.

Mirrors the Java host's ``gameval/GamevalIndex``, ``GamevalType`` and ``GamevalRef``
(JBotWithUsV2 develop 57449b45). A gameval is unique within its type and survives a game update,
so code that names ``BANK__BANK_INV_BUTTON`` or ``ACTIONBAR3_INT5_TYPE`` keeps working when the id
moves::

    from botwithus.api.gameval import GamevalIndex, GamevalType
    yew = GamevalIndex().require(GamevalType.ITEM, "YEW_LOGS")

The index is the native host's (``_native.gameval_*``, native ABI 8): the game's own index-67
tables in the **local** game cache, read without the network, one type on its first use. Names
match case-insensitively. On a host older than ABI 8, without a local cache, or with an NXTCache
that cannot read them, :meth:`GamevalIndex.is_available` is False and every lookup is None, so
code degrades rather than crashes.

The api modules address their interfaces, components and variables through :class:`GamevalRef`,
which carries the id the name resolved to when the module was written. When a name does not
resolve, the fallback is used and one warning per name says so.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from ..core.native import NativeUnavailable, backend

_log = logging.getLogger(__name__)

INTERFACE_ID_SHIFT = 16
COMPONENT_ID_MASK = 0xFFFF
DEFAULT_PREFIX_LIMIT = 100
NATIVE_ABI = 8
RETRY_UNAVAILABLE_S = 5.0


class GamevalType(enum.Enum):
    """A namespace of names; the value is the wire string the native host takes (Java's
    ``GamevalType.wire()``). ITEM is the game's ``obj`` table and VARP its ``var_player``."""

    ITEM = "item"
    NPC = "npc"
    LOC = "loc"
    INV = "inv"
    ENUM = "enum"
    STRUCT = "struct"
    PARAM = "param"
    SEQ = "seq"
    GRAPHIC = "graphic"
    QUEST = "quest"
    CATEGORY = "category"
    ACHIEVEMENT = "achievement"
    DBROW = "dbrow"
    DBTABLE = "dbtable"
    MAPELEMENT = "mapelement"
    INTERFACE = "interface"
    COMPONENT = "component"
    CURSOR = "cursor"
    HEADBAR = "headbar"
    HITMARK = "hitmark"
    STYLESHEET = "stylesheet"
    FONTMETRICS = "fontmetrics"
    UI_ANIM = "ui_anim"
    UI_ANIM_CURVE = "ui_anim_curve"
    VARP = "varp"
    VARBIT = "varbit"
    VAR_CLIENT = "var_client"
    VAR_NPC = "var_npc"
    VARBIT_NPC = "varbit_npc"
    VAR_OBJECT = "var_object"
    VARBIT_OBJECT = "varbit_object"
    VAR_CLAN = "var_clan"
    VARBIT_CLAN = "varbit_clan"
    VAR_CLAN_SETTING = "var_clan_setting"
    VARBIT_CLAN_SETTING = "varbit_clan_setting"
    VAR_PLAYER_GROUP = "var_player_group"
    MODEL = "model"
    MATERIAL = "material"
    SOUND = "sound"
    MIDI = "midi"
    BAS = "bas"

    @property
    def wire(self) -> str:
        return self.value


class GamevalNotFound(LookupError):
    """``require`` found no such name (or no index is available)."""

    def __init__(self, type_: GamevalType, gameval: str, available: bool) -> None:
        why = "" if available else " (no gameval index is available)"
        super().__init__(f"no {type_.wire} gameval named {gameval!r}{why}")
        self.type = type_
        self.gameval = gameval


class GamevalEntry(NamedTuple):
    type: GamevalType
    id: int
    gameval: str


def split_component(packed: int) -> tuple[int, int]:
    """``(interface_id, component_id)`` of a packed component id."""
    return packed >> INTERFACE_ID_SHIFT, packed & COMPONENT_ID_MASK


def pack_component(interface_id: int, component_id: int) -> int:
    return (interface_id << INTERFACE_ID_SHIFT) | component_id


class _NativeLookups:
    """The four ``_native.gameval_*`` calls, on whatever backend is installed at call time."""

    @staticmethod
    def _be() -> Any:
        try:
            be = backend()
        except NativeUnavailable:
            return None
        return be if hasattr(be, "gameval_id") else None

    def status(self) -> tuple[bool, int, str]:
        be = self._be()
        if be is None:
            return False, 0, f"this bwu_host predates gameval names (native ABI {NATIVE_ABI})"
        ok, version, reason = be.gameval_status()
        return bool(ok), int(version), str(reason)

    def id(self, wire: str, name: str) -> int | None:
        return self._be().gameval_id(wire, name)

    def name(self, wire: str, id_: int) -> str | None:
        return self._be().gameval_name(wire, id_)

    def prefix(self, wire: str, prefix: str, limit: int) -> tuple[list[tuple[int, str]], int]:
        return self._be().gameval_prefix(wire, prefix, limit)


class GamevalIndex:
    """Resolves gameval names to ids and back. Never raises for a missing name or index: a
    lookup that cannot answer is None. Safe from any thread."""

    def __init__(self, lookups: Any | None = None) -> None:
        self._lookups = lookups if lookups is not None else _NativeLookups()
        self._lock = threading.Lock()
        self._status: tuple[bool, int, str] | None = None
        self._retry_at = 0.0

    @classmethod
    def from_tables(cls, tables: dict[GamevalType, dict[str, int]]) -> "GamevalIndex":
        """An index over in-memory names (for tests): ``{type: {NAME: id}}``."""
        return cls(_TableLookups(tables))

    @classmethod
    def empty(cls) -> "GamevalIndex":
        """An index that resolves nothing; test for it with :meth:`is_available`."""
        return cls(_TableLookups(None))

    def _ready(self) -> tuple[bool, int, str]:
        with self._lock:
            now = time.monotonic()
            stale = self._status is None or (not self._status[0] and now >= self._retry_at)
            if stale:
                self._status = self._lookups.status()
                self._retry_at = now + RETRY_UNAVAILABLE_S   # a failure is asked again, not hammered
            return self._status

    def is_available(self) -> bool:
        return self._ready()[0]

    def index_version(self) -> int:
        """Index 67's version stamp (Java's gameval.sqlite ``meta.index_version``), 0 if unknown."""
        return self._ready()[1]

    def unavailable_reason(self) -> str:
        return self._ready()[2]

    def id(self, type_: GamevalType, gameval: str) -> int | None:
        if not self.is_available():
            return None
        try:
            return self._lookups.id(type_.wire, gameval)
        except RuntimeError as exc:   # a table the cache lacks, a DLL that cannot read it
            _log.debug("gameval %s %s: %s", type_.wire, gameval, exc)
            return None

    def require(self, type_: GamevalType, gameval: str) -> int:
        found = self.id(type_, gameval)
        if found is None:
            raise GamevalNotFound(type_, gameval, self.is_available())
        return found

    def gameval(self, type_: GamevalType, id_: int) -> str | None:
        if not self.is_available():
            return None
        try:
            return self._lookups.name(type_.wire, id_)
        except RuntimeError:
            return None

    def starting_with(self, type_: GamevalType, prefix: str,
                      limit: int = DEFAULT_PREFIX_LIMIT) -> list[GamevalEntry]:
        """Names in ``type_`` starting with ``prefix``, in name order; for tooling and
        diagnostics, as Java's ``startingWith``."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not self.is_available():
            return []
        try:
            rows, _total = self._lookups.prefix(type_.wire, prefix, limit)
        except RuntimeError:
            return []
        return [GamevalEntry(type_, i, n) for i, n in rows]

    def component(self, gameval: str) -> tuple[int, int] | None:
        """``(interface_id, component_id)`` of a COMPONENT name, e.g. BANK__BANK_INV_BUTTON ->
        (517, 39)."""
        packed = self.id(GamevalType.COMPONENT, gameval)
        return None if packed is None else split_component(packed)

    def interface_id(self, gameval: str) -> int | None:
        return self.id(GamevalType.INTERFACE, gameval)


class _TableLookups:
    """Lookups over ``{type: {NAME: id}}``; None tables means no index at all."""

    def __init__(self, tables: dict[GamevalType, dict[str, int]] | None) -> None:
        self._tables = None if tables is None else {
            t.wire: {n.upper(): i for n, i in names.items()} for t, names in tables.items()}

    def status(self) -> tuple[bool, int, str]:
        return (False, 0, "no gameval index") if self._tables is None else (True, 0, "")

    def id(self, wire: str, name: str) -> int | None:
        return (self._tables or {}).get(wire, {}).get(name.upper())

    def name(self, wire: str, id_: int) -> str | None:
        return next((n for n, i in (self._tables or {}).get(wire, {}).items() if i == id_), None)

    def prefix(self, wire: str, prefix: str, limit: int) -> tuple[list[tuple[int, str]], int]:
        rows = sorted((n, i) for n, i in (self._tables or {}).get(wire, {}).items()
                      if n.startswith(prefix.upper()))
        return [(i, n) for n, i in rows[:limit]], len(rows)


_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_fallback(ref: "GamevalRef", reason: str) -> None:
    key = (ref.type.wire, ref.gameval)
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    _log.warning("gameval %s %s did not resolve (%s); using its fallback id %d",
                 ref.type.wire, ref.gameval, reason, ref.fallback_id)


@dataclass(frozen=True)
class GamevalRef:
    """A gameval name and the id it resolved to when the code was written (Java's
    ``GamevalRef``). For COMPONENT the fallback is packed ``(iface << 16) | comp``."""

    type: GamevalType
    gameval: str
    fallback_id: int

    def resolve(self, index: GamevalIndex | None = None) -> int:
        """The id ``index`` gives the name, else :attr:`fallback_id` with a one-time warning."""
        index = index if index is not None else default_index()
        found = index.id(self.type, self.gameval)
        if found is not None:
            return found
        reason = "not in the index" if index.is_available() else index.unavailable_reason()
        _warn_fallback(self, reason)
        return self.fallback_id

    def component(self, index: GamevalIndex | None = None) -> tuple[int, int]:
        """``(interface_id, component_id)`` of a COMPONENT ref."""
        return split_component(self.resolve(index))


def component_ref(gameval: str, interface_id: int, component_id: int) -> GamevalRef:
    return GamevalRef(GamevalType.COMPONENT, gameval, pack_component(interface_id, component_id))


_default: GamevalIndex | None = None
_default_lock = threading.Lock()


def default_index() -> GamevalIndex:
    """The process-wide native-backed index the api modules resolve through when not handed one
    (the native index itself is process-wide: one per bwu_host process)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = GamevalIndex()
        return _default


def _reset_default_for_tests(index: GamevalIndex | None = None) -> None:
    """Replace (or clear) the default index and the warned-name memo. Tests only."""
    global _default
    with _default_lock:
        _default = index
    with _warned_lock:
        _warned.clear()

