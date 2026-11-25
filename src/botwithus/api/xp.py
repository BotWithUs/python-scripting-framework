"""Skill xp tables and an xp tracker.

Mirrors the Java host's ``util/XpTable`` and ``util/XpTracker`` (JBotWithUsV2, phase J1). The
values are v1's table, trimmed to what the 200M xp cap reaches: the standard table to level 126,
the elite table (Invention) to 120. v1's elite table stops at 120 and nothing here invents
levels past it.

**No level cap on the wire.** v1 read each skill's maximum level from client memory; the agent
publishes no such field, so :meth:`XpTable.level_for_xp` returns the *virtual* level and the
two-argument form takes the cap (99, 120, ...) from the caller.
"""
from __future__ import annotations

from bisect import bisect_left
from enum import Enum
from typing import TYPE_CHECKING, Any

from .timing import Stopwatch, per_hour

if TYPE_CHECKING:
    from .skills import Skill
    from .snapshot_types import SkillEntry

MAX_XP = 200_000_000
MIN_LEVEL = 1
STANDARD_TABLE_TOP = 126
ELITE_TABLE_TOP = 120
_MS_PER_HOUR = 3_600_000

_STANDARD = (
    0, 83, 174, 276, 388, 512, 650, 801, 969, 1_154, 1_358, 1_584, 1_833, 2_107, 2_411,
    2_746, 3_115, 3_523, 3_973, 4_470, 5_018, 5_624, 6_291, 7_028, 7_842, 8_740, 9_730,
    10_824, 12_031, 13_363, 14_833, 16_456, 18_247, 20_224, 22_406, 24_815, 27_473,
    30_408, 33_648, 37_224, 41_171, 45_529, 50_339, 55_649, 61_512, 67_983, 75_127,
    83_014, 91_721, 101_333, 111_945, 123_660, 136_594, 150_872, 166_636, 184_040,
    203_254, 224_466, 247_886, 273_742, 302_288, 333_804, 368_599, 407_015, 449_428,
    496_254, 547_953, 605_032, 668_051, 737_627, 814_445, 899_257, 992_895, 1_096_278,
    1_210_421, 1_336_443, 1_475_581, 1_629_200, 1_798_808, 1_986_068, 2_192_818,
    2_421_087, 2_673_114, 2_951_373, 3_258_594, 3_597_792, 3_972_294, 4_385_776,
    4_842_295, 5_346_332, 5_902_831, 6_517_253, 7_195_629, 7_944_614, 8_771_558,
    9_684_577, 10_692_629, 11_805_606, 13_034_431, 14_391_160, 15_889_109, 17_542_976,
    19_368_992, 21_385_073, 23_611_006, 26_068_632, 28_782_068, 31_777_942, 35_085_654,
    38_737_661, 42_769_801, 47_221_641, 52_136_869, 57_563_718, 63_555_443, 70_170_840,
    77_474_828, 85_539_082, 94_442_736, 104_273_167, 115_126_838, 127_110_260,
    140_341_028, 154_948_977, 171_077_457, 188_884_740
)

_ELITE = (
    0, 830, 1_861, 2_902, 3_980, 5_126, 6_390, 7_787, 9_400, 11_275, 13_605, 16_372,
    19_656, 23_546, 28_138, 33_520, 39_809, 47_109, 55_535, 64_802, 77_190, 90_811,
    106_221, 123_573, 143_025, 164_742, 188_893, 215_651, 245_196, 277_713, 316_311,
    358_547, 404_634, 454_796, 509_259, 568_254, 632_019, 700_797, 774_834, 854_383,
    946_227, 1_044_569, 1_149_696, 1_261_903, 1_381_488, 1_508_756, 1_644_015,
    1_787_581, 1_939_773, 2_100_917, 2_283_490, 2_476_369, 2_679_907, 2_894_505,
    3_120_508, 3_358_307, 3_608_290, 3_870_846, 4_146_374, 4_435_275, 4_758_122,
    5_096_111, 5_449_685, 5_819_299, 6_205_407, 6_608_473, 7_028_964, 7_467_354,
    7_924_122, 8_399_751, 8_925_664, 9_472_665, 10_041_285, 10_632_061, 11_245_538,
    11_882_262, 12_542_789, 13_227_679, 13_937_496, 14_672_812, 15_478_994, 16_313_404,
    17_176_661, 18_069_395, 18_992_239, 19_945_833, 20_930_821, 21_947_856, 22_997_593,
    24_080_695, 25_259_906, 26_475_754, 27_728_955, 29_020_233, 30_350_318, 31_719_944,
    33_129_852, 34_580_790, 36_073_511, 37_608_773, 39_270_442, 40_978_509, 42_733_789,
    44_537_107, 46_389_292, 48_291_180, 50_243_611, 52_247_435, 54_303_504, 56_412_678,
    58_575_823, 60_793_812, 63_067_521, 65_397_835, 67_785_643, 70_231_841, 72_737_330,
    75_303_019, 77_929_820, 80_618_654
)


class XpTable(Enum):
    """The xp needed for each level: ``STANDARD`` for most skills, ``ELITE`` for Invention."""

    STANDARD = _STANDARD
    ELITE = _ELITE

    @property
    def max_level(self) -> int:
        return len(self.value)

    def xp_for_level(self, level: int) -> int:
        """The xp at which ``level`` starts. ValueError outside ``1..max_level``."""
        if not MIN_LEVEL <= level <= self.max_level:
            raise ValueError(f"level {level} is outside 1..{self.max_level} for {self.name}")
        return self.value[level - 1]

    def level_for_xp(self, xp: int, level_cap: int | None = None) -> int:
        """The (virtual) level ``xp`` reaches, capped at ``level_cap`` when given."""
        table = self.value
        index = bisect_left(table, xp)
        exact = index < len(table) and table[index] == xp
        level = max(MIN_LEVEL, index + 1 if exact else index)
        return level if level_cap is None else min(level, level_cap)

    def xp_to_next_level(self, xp: int) -> int:
        """Xp left to the next level; 0 at or past the table's top."""
        level = self.level_for_xp(xp)
        if level >= self.max_level:
            return 0
        return self.xp_for_level(level + 1) - max(0, xp)


def _stat(game: Any, skill: "Skill") -> "SkillEntry | None":
    player = game.snapshot().self_player
    if not player.valid:
        return None
    for entry in player.skills:
        if entry.type_id == int(skill):
            return entry
    return None


class XpTracker:
    """Xp and levels gained in one skill since it was made (or :meth:`reset`).

    Reads the local player's skills from the snapshot. When the skill cannot be read at
    construction (logged out, no frame), the baseline is taken at the first readable call and the
    clock restarts there.
    """

    def __init__(self, game: Any, skill: "Skill", stopwatch: Stopwatch | None = None) -> None:
        self._game = game
        self.skill = skill
        self._stopwatch = stopwatch if stopwatch is not None else Stopwatch.started()
        self._baseline = _stat(game, skill)

    def _current(self) -> "SkillEntry | None":
        now = _stat(self._game, self.skill)
        if self._baseline is None and now is not None:
            self._baseline = now
            self._stopwatch.reset()
        return now

    def elapsed_ms(self) -> int:
        self._current()
        return self._stopwatch.elapsed_ms()

    def xp_gained(self) -> int:
        now = self._current()
        if now is None or self._baseline is None:
            return 0
        return now.experience - self._baseline.experience

    def levels_gained(self) -> int:
        now = self._current()
        if now is None or self._baseline is None:
            return 0
        return now.actual_level - self._baseline.actual_level

    def xp_per_hour(self) -> int:
        return per_hour(self.xp_gained(), self.elapsed_ms())

    def time_to_next_level_ms(self) -> int | None:
        """Ms to the next level at the current rate; None with no rate or at the table's top."""
        now = self._current()
        rate = self.xp_per_hour()
        if now is None or rate <= 0:
            return None
        remaining = self.skill.xp_table.xp_to_next_level(now.experience)
        if remaining == 0:
            return None
        return remaining * _MS_PER_HOUR // rate

    def reset(self) -> None:
        self._baseline = _stat(self._game, self.skill)
        self._stopwatch.reset()
