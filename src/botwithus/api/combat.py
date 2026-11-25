"""The local player's combat state: adrenaline, life, prayer and summoning points, own name, the
resolved combat target, "in combat", and a per-entity history of recent hitmarks.

Mirrors the Java host's ``combat/LocalCombat``, ``CombatTarget`` and ``HitmarkHistory``
(JBotWithUsV2, phase J2). Hitmarks are the agent's event 70 (:class:`~botwithus.api.events.
Hitmark`), which the agent emits from NXTLibrary main ``3e20928e`` (nxt-library #107).

``is_in_combat`` is a documented heuristic, as in the Java host: the game's
``COMBATV2_PLAYER_IS_IN_COMBAT`` flag, a current target, or a hitmark on self in the last
:data:`RECENT_HIT_MS` (only when a :class:`HitmarkHistory` is passed).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Union

from .events import EventBus, Hitmark
from .gameval import GamevalIndex, GamevalRef, GamevalType
from .prayers import Prayers
from .snapshot_types import NpcEntry, PlayerEntry

if TYPE_CHECKING:
    from .game import GameApi

TARGET_TYPE_CLEARED = 0        # after a kill
TARGET_TYPE_NPC = 1            # verified live
TARGET_TYPE_PLAYER = 2         # inferred, not verified live (as in the Java host)
TARGET_TYPE_NEVER_SET = 127    # never set this session
NO_TARGET_INDEX = -1
RECENT_HIT_MS = 6_000

VARP_ADRENALINE = 679          # SA_ENERGY
VARBIT_IN_COMBAT = 1899        # COMBATV2_PLAYER_IS_IN_COMBAT
VARBIT_SUMMONING_POINTS = 41524  # LORE_POINTS (tenths)
VARP_LIFE_POINTS = 13537       # LIFEPOINTS
VARP_MAX_LIFE_POINTS = 13538   # MAXLIFEPOINTS
# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
ADRENALINE_REF = GamevalRef(GamevalType.VARP, "SA_ENERGY", VARP_ADRENALINE)
IN_COMBAT_REF = GamevalRef(GamevalType.VARBIT, "COMBATV2_PLAYER_IS_IN_COMBAT", VARBIT_IN_COMBAT)
SUMMONING_POINTS_REF = GamevalRef(GamevalType.VARBIT, "LORE_POINTS", VARBIT_SUMMONING_POINTS)
LIFE_POINTS_REF = GamevalRef(GamevalType.VARP, "LIFEPOINTS", VARP_LIFE_POINTS)
MAX_LIFE_POINTS_REF = GamevalRef(GamevalType.VARP, "MAXLIFEPOINTS", VARP_MAX_LIFE_POINTS)

HITMARK_ON_PLAYER = 0          # HitmarkBody.targetType (NXTLibrary Events.h)
HITMARK_ON_NPC = 1

_IN_COMBAT = 1
_TENTHS = 10


@dataclass(frozen=True, slots=True)
class NoTarget:
    pass


@dataclass(frozen=True, slots=True)
class NpcTarget:
    npc: NpcEntry


@dataclass(frozen=True, slots=True)
class PlayerTarget:
    player: PlayerEntry


@dataclass(frozen=True, slots=True)
class UnresolvedTarget:
    """A target the frame does not hold (out of view), or of a type this does not know."""

    target_index: int
    target_type: int


CombatTarget = Union[NoTarget, NpcTarget, PlayerTarget, UnresolvedTarget]
NO_TARGET = NoTarget()


@dataclass(frozen=True, slots=True)
class TimedHitmark:
    """A hitmark event with the wall time (ms) it was seen at."""

    hit: Hitmark
    timestamp_ms: int


class HitmarkHistory:
    """The last hitmarks per entity (up to :data:`PER_ENTITY_CAPACITY`, for
    :data:`RETENTION_MS`). :meth:`attach` subscribes it to a bus; :meth:`close` detaches it."""

    PER_ENTITY_CAPACITY = 16
    RETENTION_MS = 30_000

    def __init__(self, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._by_entity: dict[tuple[int, int], deque[TimedHitmark]] = {}
        self._off: Callable[[], None] | None = None

    @classmethod
    def attach(cls, bus: EventBus, clock_ms: Callable[[], int] | None = None) -> "HitmarkHistory":
        history = cls(clock_ms)
        history._off = bus.subscribe(Hitmark, history.record)
        return history

    def close(self) -> None:
        if self._off is not None:
            self._off()
            self._off = None
        with self._lock:
            self._by_entity.clear()

    def record(self, hit: Hitmark) -> None:
        now = self._clock()
        cutoff = now - self.RETENTION_MS
        key = (hit.target_type, hit.target_server_index)
        with self._lock:
            hits = self._by_entity.setdefault(key, deque())
            hits.append(TimedHitmark(hit, now))
            while hits and (len(hits) > self.PER_ENTITY_CAPACITY or hits[0].timestamp_ms < cutoff):
                hits.popleft()

    def recent(self, target_type: int, server_index: int,
               within_ms: int | None = None) -> list[Hitmark]:
        """Hitmarks on the entity (:data:`HITMARK_ON_PLAYER` / :data:`HITMARK_ON_NPC`, server
        index) seen in the last ``within_ms`` (default: :attr:`RETENTION_MS`)."""
        cutoff = self._clock() - (self.RETENTION_MS if within_ms is None else within_ms)
        with self._lock:
            hits = self._by_entity.get((target_type, server_index), ())
            return [h.hit for h in hits if h.timestamp_ms >= cutoff]

    def damage_within(self, target_type: int, server_index: int, within_ms: int) -> int:
        return sum(h.damage for h in self.recent(target_type, server_index, within_ms))


class LocalCombat:
    """Combat reads for one game connection."""

    def __init__(self, game: "GameApi", prayers: Prayers | None = None,
                 gamevals: GamevalIndex | None = None) -> None:
        self._game = game
        self._gamevals = gamevals
        self._prayers = prayers or Prayers(game, gamevals=gamevals)

    def adrenaline(self) -> int:
        return self._game.varp(ADRENALINE_REF.resolve(self._gamevals))

    def life_points(self) -> int:
        health = self._game.local_health()
        return health[0] if health is not None else self._game.varp(LIFE_POINTS_REF.resolve(self._gamevals))

    def max_life_points(self) -> int:
        health = self._game.local_health()
        return health[1] if health is not None else self._game.varp(MAX_LIFE_POINTS_REF.resolve(self._gamevals))

    def prayer_points(self) -> int:
        return self._prayers.points()

    def summoning_points(self) -> int:
        raw = self._game.varbit(SUMMONING_POINTS_REF.resolve(self._gamevals))
        return -1 if raw < 0 else raw // _TENTHS

    def name(self) -> str | None:
        """The local player's display name, as the players array has it; None until decoded."""
        snap = self._game.snapshot()
        own = snap.own_index
        player = next((p for p in snap.players() if p.server_index == own), None)
        return player.name if player is not None and player.name else None

    def target(self) -> CombatTarget:
        snap = self._game.snapshot()
        me = snap.self_player
        if not me.valid or not _has_target(me.target_index, me.target_type):
            return NO_TARGET
        index, kind = me.target_index, me.target_type
        if kind == TARGET_TYPE_NPC:
            npc = next((n for n in snap.npcs() if n.server_index == index), None)
            return NpcTarget(npc) if npc is not None else UnresolvedTarget(index, kind)
        if kind == TARGET_TYPE_PLAYER:
            player = next((p for p in snap.players() if p.server_index == index), None)
            return PlayerTarget(player) if player is not None else UnresolvedTarget(index, kind)
        return UnresolvedTarget(index, kind)

    def is_in_combat(self, hitmarks: HitmarkHistory | None = None) -> bool:
        if self._game.varbit(IN_COMBAT_REF.resolve(self._gamevals)) == _IN_COMBAT:
            return True
        me = self._game.snapshot().self_player
        if not me.valid:
            return False
        if _has_target(me.target_index, me.target_type):
            return True
        return hitmarks is not None and bool(
            hitmarks.recent(HITMARK_ON_PLAYER, me.server_index, RECENT_HIT_MS))


def _has_target(index: int, kind: int) -> bool:
    return (index != NO_TARGET_INDEX and kind != TARGET_TYPE_CLEARED
            and kind != TARGET_TYPE_NEVER_SET)
