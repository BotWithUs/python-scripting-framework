"""J1 Coordinate / Area shapes, the xp tables, SkillType and the XpTracker."""
import random

import pytest

from botwithus.api.skills import Skill, SkillType
from botwithus.api.timing import Stopwatch
from botwithus.api.world import (CircularArea, Coordinate, PolygonalArea, RectangularArea)
from botwithus.api.xp import XpTable, XpTracker

from fake_game import FakeGame, FakeSnapshot, me, npc, skill


# --- Coordinate -------------------------------------------------------------------------

def test_region_ids_use_x_high_and_y_low():
    # Failure: x and y swapped (Lumbridge is (50, 50), where a swap is invisible; Varrock is not).
    lumbridge = Coordinate(3222, 3218)
    varrock = Coordinate(3212, 3424)
    assert (lumbridge.region_x, lumbridge.region_y) == (50, 50)
    assert (varrock.region_x, varrock.region_y) == (50, 53)
    assert varrock.region_id == (50 << 8) | 53
    assert (varrock.region_local_x, varrock.region_local_y) == (3212 - 3200, 3424 - 3392)


def test_distances_and_offsets():
    a = Coordinate(10, 10, 1)
    assert a.chebyshev_distance(Coordinate(13, 14, 1)) == 4
    assert a.euclidean_distance(Coordinate(13, 14, 1)) == 5.0
    assert a.offset(2, -3, 1) == Coordinate(12, 7, 2)
    with pytest.raises(ValueError):
        Coordinate(1, 1, 4)


# --- Areas ------------------------------------------------------------------------------

def test_rectangle_checks_the_plane_and_both_edges():
    # Failure: a rectangle that ignores the plane, or excludes its own edge tiles.
    r = RectangularArea(100, 200, 104, 203, 2)
    assert r.contains(Coordinate(100, 200, 2)) and r.contains(Coordinate(104, 203, 2))
    assert not r.contains(Coordinate(102, 201, 0))
    assert not r.contains(Coordinate(105, 201, 2))
    assert (r.width, r.height) == (5, 4)


def test_a_rectangle_tests_entity_rows_and_converts_to_an_avoid_area():
    r = RectangularArea.of(Coordinate(10, 20), Coordinate(5, 15))
    assert r == RectangularArea(5, 15, 10, 20, 0)
    assert npc(1, 1, 7, 17) in r and npc(1, 1, 11, 17) not in r
    avoid = r.to_avoid()
    assert (avoid.min_x, avoid.max_y, avoid.plane) == (5, 20, 0)


def test_circle_uses_euclidean_radius():
    # Failure: a Chebyshev circle would hold the (3, 3) corner.
    c = CircularArea(Coordinate(50, 50, 1), 3)
    assert c.contains(Coordinate(53, 50, 1)) and c.contains(Coordinate(52, 52, 1))
    assert not c.contains(Coordinate(53, 53, 1))
    assert not c.contains(Coordinate(50, 50, 0))


def test_polygon_includes_its_outline_and_uses_even_odd_inside():
    # An L shape. Failure: an edge or vertex tile reported outside, or the notch inside.
    poly = PolygonalArea.of(Coordinate(0, 0), Coordinate(6, 0), Coordinate(6, 3),
                            Coordinate(3, 3), Coordinate(3, 6), Coordinate(0, 6))
    assert poly.contains(Coordinate(0, 0)) and poly.contains(Coordinate(6, 3))
    assert poly.contains(Coordinate(4, 3)) and poly.contains(Coordinate(1, 5))
    assert not poly.contains(Coordinate(5, 5))
    assert not poly.contains(Coordinate(7, 1))
    assert len(poly.tiles()) == 7 * 4 + 4 * 3
    with pytest.raises(ValueError):
        PolygonalArea.of(Coordinate(0, 0, 0), Coordinate(1, 0, 1), Coordinate(1, 1, 0))


@pytest.mark.parametrize("area", [
    RectangularArea(10, 10, 12, 30, 1),
    CircularArea(Coordinate(100, 100, 0), 4),
    PolygonalArea.of(Coordinate(0, 0), Coordinate(9, 0), Coordinate(0, 9)),
])
def test_random_tiles_are_always_inside(area):
    rng = random.Random(3)
    assert all(area.contains(area.random_tile(rng)) for _ in range(300))


class _Walker:
    def __init__(self, reachable):
        self.reachable = reachable
        self.asked = []

    def is_reachable(self, tile):
        self.asked.append(tile)
        return (tile.x, tile.y) in self.reachable


def test_random_reachable_tile_asks_the_walker_and_stops_at_attempts():
    area = RectangularArea(0, 0, 1, 0, 0)
    assert area.random_reachable_tile(_Walker({(1, 0)}), 50, random.Random(1)) \
        == Coordinate(1, 0, 0)
    never = _Walker(set())
    assert area.random_reachable_tile(never, 4, random.Random(1)) is None
    assert len(never.asked) == 4


# --- xp tables --------------------------------------------------------------------------

@pytest.mark.parametrize("level,xp", [(1, 0), (2, 83), (10, 1154), (50, 101_333),
                                      (92, 6_517_253), (99, 13_034_431), (120, 104_273_167),
                                      (126, 188_884_740)])
def test_standard_anchor_levels(level, xp):
    # Failure: any anchor off by any amount.
    assert XpTable.STANDARD.xp_for_level(level) == xp
    assert XpTable.STANDARD.level_for_xp(xp) == level
    assert XpTable.STANDARD.level_for_xp(xp - 1) == max(1, level - 1)


@pytest.mark.parametrize("level,xp", [(2, 830), (99, 36_073_511), (120, 80_618_654)])
def test_elite_anchor_levels(level, xp):
    assert XpTable.ELITE.xp_for_level(level) == xp
    assert XpTable.ELITE.level_for_xp(xp) == level


def test_caps_tops_and_xp_to_next():
    assert XpTable.STANDARD.level_for_xp(200_000_000) == 126
    assert XpTable.STANDARD.level_for_xp(200_000_000, 99) == 99
    assert XpTable.ELITE.max_level == 120 and XpTable.STANDARD.max_level == 126
    assert XpTable.STANDARD.xp_to_next_level(100) == 74     # 174 - 100
    assert XpTable.STANDARD.xp_to_next_level(190_000_000) == 0
    with pytest.raises(ValueError):
        XpTable.ELITE.xp_for_level(121)


def test_skill_type_carries_name_elite_flag_and_table():
    # Failure: Invention not elite, or a display name that is not the game's.
    assert SkillType is Skill
    assert Skill.INVENTION.is_elite and Skill.INVENTION.xp_table is XpTable.ELITE
    assert not Skill.NECROMANCY.is_elite and Skill.NECROMANCY.xp_table is XpTable.STANDARD
    assert Skill.RUNECRAFTING.display_name == "Runecrafting"
    assert Skill.by_id(16) is Skill.AGILITY and Skill.by_id(29) is None


# --- XpTracker --------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def test_xp_tracker_gains_rate_and_time_to_level():
    # Failure: gains measured from 0 instead of the baseline, or a rate not per hour.
    clock = _Clock()
    game = FakeGame(FakeSnapshot(self_player=me(skills=[skill(8, 60, 273_742)])))
    tracker = XpTracker(game, Skill.WOODCUTTING, Stopwatch.started(clock))
    game.snap.self_player = me(skills=[skill(8, 61, 303_742)])
    clock.now = 30 * 60 * 1_000_000_000
    assert tracker.xp_gained() == 30_000
    assert tracker.levels_gained() == 1
    assert tracker.xp_per_hour() == 60_000
    # level 62 starts at 333,804
    assert tracker.time_to_next_level_ms() == (333_804 - 303_742) * 3_600_000 // 60_000


def test_xp_tracker_baselines_at_the_first_readable_call():
    # Failure: a tracker made while logged out counting all xp as gained, or keeping the old
    # clock (the rate would be diluted by the logged-out time).
    clock = _Clock()
    game = FakeGame(FakeSnapshot(self_player=me(skills=[])))
    tracker = XpTracker(game, Skill.MINING, Stopwatch.started(clock))
    clock.now = 10 * 60 * 1_000_000_000
    game.snap.self_player = me(skills=[skill(14, 70, 737_627)])
    assert tracker.xp_gained() == 0
    assert tracker.elapsed_ms() == 0
    game.snap.self_player = me(skills=[skill(14, 70, 747_627)])
    clock.now += 60 * 60 * 1_000_000_000
    assert tracker.xp_gained() == 10_000 and tracker.xp_per_hour() == 10_000
