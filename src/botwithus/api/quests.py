"""Quest status, progress and unmet requirements, from the quest's cache definition and its
progress variable.

Mirrors the Java host's ``quest/QuestLog``, ``QuestStatus``, ``QuestStage`` and ``QuestVar``
(JBotWithUsV2, phase J3).

* **Stage.** A quest's first progress variable (varps first, then varbits, as the definition
  lists them) carries ``(start value, end value)``. At or above the end it is COMPLETE; **at or
  above** the start it is STARTED (not above it, as v1 had: Cook's Assistant is start 1,
  end 2); below, NOT_STARTED. A quest with no progress variable, or one whose variable cannot
  be read, is UNKNOWN, never guessed.
* **Stub records.** Some ids are unlisted stubs (empty ``listName``): Cook's Assistant 6 and
  Vampyre Slayer 132. Asked about one, the log reads the listed quest of the same name instead
  (257 and 258). The name index is built once from the quest ids the gameval index names
  (native ABI 8), as the Java host does. Without a gameval index it falls back to scanning quest
  ids from 0 until :data:`QUEST_SCAN_GAP` ids in a row are missing (at most
  :data:`QUEST_SCAN_MAX`), and says so once.
* **Requirements** checked: membership, skill levels and prior quests. Quest points are not:
  v1's varp 101 is ``PAGEPOS`` in the gameval index, and the definitions' varp/varbit
  requirements are not decoded by NXTCache.
"""
from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

from .cache_values import CacheValues
from .gameval import GamevalIndex, GamevalType, default_index

if TYPE_CHECKING:
    from .game import GameApi

QUEST_SCAN_MAX = 4096           # also the Java host's MAX_QUESTS for the gameval listing
QUEST_SCAN_GAP = 64
UNKNOWN_PROGRESS = -1
_TRIPLE = 3

_log = logging.getLogger(__name__)


class QuestStage(enum.Enum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class VarKind(enum.Enum):
    VARP = "varp"
    VARBIT = "varbit"


@dataclass(frozen=True, slots=True)
class QuestVar:
    kind: VarKind
    id: int
    start_value: int
    end_value: int


@dataclass(frozen=True, slots=True)
class QuestStatus:
    quest_id: int
    name: str
    stage: QuestStage
    progress: int
    variable: QuestVar | None = None

    @property
    def is_started(self) -> bool:
        return self.stage in (QuestStage.STARTED, QuestStage.COMPLETE)

    @property
    def is_complete(self) -> bool:
        return self.stage is QuestStage.COMPLETE


@dataclass(frozen=True, slots=True)
class MembershipRequired:
    pass


@dataclass(frozen=True, slots=True)
class SkillRequired:
    skill_id: int
    level: int
    actual: int


@dataclass(frozen=True, slots=True)
class PriorQuestRequired:
    quest_id: int


QuestRequirement = Union[MembershipRequired, SkillRequired, PriorQuestRequired]


def stage_of(variable: QuestVar, value: int) -> QuestStage:
    if value >= variable.end_value:
        return QuestStage.COMPLETE
    return QuestStage.STARTED if value >= variable.start_value else QuestStage.NOT_STARTED


def progress_vars(quest: dict[str, Any]) -> list[QuestVar]:
    """The quest's progress variables, varps first then varbits; malformed entries skipped."""
    out: list[QuestVar] = []
    for key, kind in (("progressVarps", VarKind.VARP), ("progressVarbits", VarKind.VARBIT)):
        for triple in quest.get(key) or ():
            if (isinstance(triple, list) and len(triple) >= _TRIPLE
                    and all(isinstance(v, int) for v in triple[:_TRIPLE])):
                out.append(QuestVar(kind, triple[0], triple[1], triple[2]))
    return out


def _is_listed(quest: dict[str, Any]) -> bool:
    return bool(quest.get("listName"))


class QuestLog:
    """Quest reads for one game connection."""

    def __init__(self, game: "GameApi", cache: CacheValues | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._cache = cache or CacheValues()
        self._gamevals = gamevals
        self._listed: dict[str, int] | None = None
        self._lock = threading.Lock()

    def status(self, quest_id: int) -> QuestStatus:
        quest = self._quest(quest_id)
        if quest is None:
            return QuestStatus(quest_id, "", QuestStage.UNKNOWN, UNKNOWN_PROGRESS)
        quest_id, name = int(quest.get("id", quest_id)), str(quest.get("name") or "")
        variables = progress_vars(quest)
        if not variables:
            return QuestStatus(quest_id, name, QuestStage.UNKNOWN, UNKNOWN_PROGRESS)
        variable = variables[0]
        value = self._read(variable)
        if value is None:
            return QuestStatus(quest_id, name, QuestStage.UNKNOWN, UNKNOWN_PROGRESS)
        return QuestStatus(quest_id, name, stage_of(variable, value), value, variable)

    def is_started(self, quest_id: int) -> bool:
        return self.status(quest_id).is_started

    def is_complete(self, quest_id: int) -> bool:
        return self.status(quest_id).is_complete

    def progress(self, quest_id: int) -> int:
        return self.status(quest_id).progress

    def unmet_requirements(self, quest_id: int) -> list[QuestRequirement]:
        quest = self._quest(quest_id)
        if quest is None:
            return []
        unmet: list[QuestRequirement] = []
        me = self._game.snapshot().self_player
        if quest.get("membersOnly") and me.valid and not me.is_member:
            unmet.append(MembershipRequired())
        levels = {s.type_id: s.actual_level for s in me.skills} if me.valid else {}
        for entry in quest.get("skillRequirements") or ():
            skill, level = entry.get("skill"), entry.get("level")
            if isinstance(skill, int) and isinstance(level, int):
                actual = levels.get(skill, 0)
                if actual < level:
                    unmet.append(SkillRequired(skill, level, actual))
        for prior in quest.get("dependentQuestIds") or ():
            if isinstance(prior, int) and not self.is_complete(prior):
                unmet.append(PriorQuestRequired(prior))
        return unmet

    def requirements_met(self, quest_id: int) -> bool:
        return not self.unmet_requirements(quest_id)

    # --- helpers ----------------------------------------------------------------------

    def _quest(self, quest_id: int) -> dict[str, Any] | None:
        """The quest's definition, the listed twin of a stub preferred."""
        quest = self._cache.quest(quest_id)
        if quest is None or _is_listed(quest):
            return quest
        listed = self._listed_by_name().get(str(quest.get("name") or ""))
        if listed is None or listed == quest_id:
            return quest
        return self._cache.quest(listed) or quest

    def _listed_by_name(self) -> dict[str, int]:
        with self._lock:
            if self._listed is None:
                self._listed = self._scan_listed()
            return self._listed

    def _scan_listed(self) -> dict[str, int]:
        index = self._gamevals if self._gamevals is not None else default_index()
        named = index.starting_with(GamevalType.QUEST, "", QUEST_SCAN_MAX)
        if named:
            found: dict[str, int] = {}
            for entry in named:   # name order, first listed quest per name wins, as Java
                quest = self._cache.quest(entry.id)
                if quest is not None and _is_listed(quest):
                    found.setdefault(str(quest.get("name") or ""), entry.id)
            return found
        reason = "no quest names" if index.is_available() else index.unavailable_reason()
        _log.warning("quest ids are not listed by the gameval index (%s); scanning ids instead", reason)
        return self._scan_ids()

    def _scan_ids(self) -> dict[str, int]:
        found: dict[str, int] = {}
        misses = 0
        for quest_id in range(QUEST_SCAN_MAX):
            quest = self._cache.quest(quest_id)
            if quest is None:
                misses += 1
                if misses >= QUEST_SCAN_GAP:
                    break
                continue
            misses = 0
            if _is_listed(quest):
                found.setdefault(str(quest.get("name") or ""), quest_id)
        return found

    def _read(self, variable: QuestVar) -> int | None:
        if variable.kind is VarKind.VARP:
            read = self._game.read_varp(variable.id)
        else:
            read = self._game.read_varbit(variable.id)
        return read.value if read.has_value else None
