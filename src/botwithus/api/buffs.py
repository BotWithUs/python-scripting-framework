"""Buffs: overloads, antifires, incenses and the flag-only effects, from their varbits.

Mirrors the Java host's ``buff/Buffs``, ``Buff`` and ``BuffState`` (JBotWithUsV2, phase J3). A
timed buff's varbit counts :data:`TIME_UNIT_S`-second units (xapi's unit; not checked live).
Aggression and the two prayer renewals are flags only, with no time left. xapi's "Powder of
Protection" and "Weapon poison" are left out, as in the Java host: their varbits are named for
something else in the gameval index.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .gameval import GamevalRef, GamevalType

if TYPE_CHECKING:
    from .game import GameApi

TIME_UNIT_S = 15

# Each buff's (varbit gameval, potency varbit gameval or None), as the Java host's Buff names them;
# the ids in the enum are the fallbacks (native ABI 8).
_GAMEVALS: dict[str, tuple[str, str | None]] = {
    "OVERLOAD": ("OVERLOAD_POTION_EFFECTS_LONG", None),
    "ANTIFIRE": ("DRAGONRESIST_ORIGINAL", None),
    "SUPER_ANTIFIRE": ("DRAGONRESIST_ALL", None),
    "IRIT_INCENSE": ("INCENSE_IRIT_ACTIVE", "INCENSE_IRIT_POTENCY"),
    "KWUARM_INCENSE": ("INCENSE_KWUARM_ACTIVE", "INCENSE_KWUARM_POTENCY"),
    "LANTADYME_INCENSE": ("INCENSE_LANTADYME_ACTIVE", "INCENSE_LANTADYME_POTENCY"),
    "AGGRESSION": ("WILDERNESS_AGGRESSION_POTION_EFFECTS", None),
    "PRAYER_RENEWAL": ("COMBATV2_TARGET_SIDE_PANEL_PLAYER_PRAYER_RENEW", None),
    "SUPER_PRAYER_RENEWAL": ("COMBATV2_TARGET_SIDE_PANEL_PLAYER_SUPER_PRAYERRENEW", None),
}


class Buff(enum.Enum):
    """``(varbit, timed, potency varbit or None)``."""

    OVERLOAD = (48834, True, None)                 # OVERLOAD_POTION_EFFECTS_LONG
    ANTIFIRE = (497, True, None)                   # DRAGONRESIST_ORIGINAL
    SUPER_ANTIFIRE = (498, True, None)             # DRAGONRESIST_ALL
    IRIT_INCENSE = (43714, True, 43696)            # INCENSE_IRIT_ACTIVE / _POTENCY
    KWUARM_INCENSE = (43725, True, 43707)
    LANTADYME_INCENSE = (43717, True, 43699)
    AGGRESSION = (33448, False, None)              # WILDERNESS_AGGRESSION_POTION_EFFECTS
    PRAYER_RENEWAL = (2100, False, None)
    SUPER_PRAYER_RENEWAL = (25852, False, None)

    @property
    def varbit_ref(self) -> GamevalRef:
        return GamevalRef(GamevalType.VARBIT, _GAMEVALS[self.name][0], self.value[0])

    @property
    def potency_ref(self) -> GamevalRef | None:
        potency = _GAMEVALS[self.name][1]
        return None if potency is None else GamevalRef(GamevalType.VARBIT, potency, self.value[2])

    @property
    def varbit(self) -> int:
        """The varbit, by its gameval name; the fallback id when it does not resolve."""
        return self.varbit_ref.resolve()

    @property
    def is_timed(self) -> bool:
        return self.value[1]

    @property
    def potency_varbit(self) -> int | None:
        ref = self.potency_ref
        return None if ref is None else ref.resolve()


@dataclass(frozen=True, slots=True)
class BuffState:
    buff: Buff
    is_active: bool
    remaining_s: int | None       # timed buffs only
    potency: int | None           # incenses only
    raw_value: int


class Buffs:
    def __init__(self, game: "GameApi") -> None:
        self._game = game

    def state(self, buff: Buff) -> BuffState:
        value = self._game.varbit(buff.varbit)
        active = value > 0
        remaining = value * TIME_UNIT_S if active and buff.is_timed else None
        potency = (None if buff.potency_varbit is None
                   else self._game.varbit(buff.potency_varbit))
        return BuffState(buff, active, remaining, potency, value)

    def is_active(self, buff: Buff) -> bool:
        return self._game.varbit(buff.varbit) > 0

    def remaining_s(self, buff: Buff) -> int | None:
        return self.state(buff).remaining_s

    def active(self) -> list[Buff]:
        reads = self._game.read_varbits([b.varbit for b in Buff])
        return [b for b, r in zip(Buff, reads) if r.value > 0]
