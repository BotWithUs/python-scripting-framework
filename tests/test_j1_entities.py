"""J1 entity query filters and terminals, the cache-backed names/options, and is_reachable."""
import random
from dataclasses import dataclass

from botwithus.api.entities import GroundItems, Npcs, Players, SceneObjects
from botwithus.api.interact import EntityDefs, interact
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import GroundItem, Tile
from botwithus.api.world import Coordinate, RectangularArea
from botwithus.core.native import PathError

from fake_game import FakeGame, FakeSnapshot, loc, npc, player

GOBLIN, COW, GUARD = 100, 81, 3010


def _snap():
    return FakeSnapshot(npc_rows=[
        npc(1, GOBLIN, 3205, 3200, hp=50, max_hp=100, anim=6184, combat=5, moving=True),
        npc(2, GOBLIN, 3210, 3200, hp=100, max_hp=100, stance=4, combat=5),
        npc(3, COW, 3201, 3203, hp=0, max_hp=0, spot=90, combat=2),
        npc(4, GUARD, 3230, 3200, plane=1, hp=70, max_hp=140, combat=21, active=GUARD + 1),
        npc(5, 999, 3202, 3202, active=-1),      # morph-hidden
    ])


def _ids(rows):
    return [r.server_index for r in rows]


def test_animation_stance_spot_and_movement_filters():
    # Failure: a filter that keeps rows it should drop (or reads the wrong field).
    q = lambda: Npcs(_snap())  # noqa: E731
    assert _ids(q().animating().all()) == [1]
    assert _ids(q().not_animating().all()) == [2, 3, 4, 5]
    assert _ids(q().with_animation(6184, 1).all()) == [1]
    assert _ids(q().with_stance(4).all()) == [2]
    assert _ids(q().with_spot_anim(90).all()) == [3]
    assert _ids(q().moving().all()) == [1]
    assert _ids(q().not_moving().all()) == [2, 3, 4, 5]
    assert _ids(q().with_combat_level(3, 21).all()) == [1, 2, 4]


def test_health_filters_skip_an_unknown_max():
    # Failure: the cow (max 0) matching a percent range, or hp compared as a percent.
    assert _ids(Npcs(_snap()).with_health(60, 100).all()) == [2, 4]
    assert _ids(Npcs(_snap()).with_health_percent(0, 50).all()) == [1, 4]


def test_with_ids_matches_base_or_active_type_but_never_minus_one():
    assert _ids(Npcs(_snap()).with_ids(GOBLIN, GUARD + 1).all()) == [1, 2, 4]
    assert _ids(Npcs(_snap()).with_ids(-1).all()) == []


def test_inside_outside_and_plane():
    area = RectangularArea(3200, 3199, 3206, 3204, 0)
    assert _ids(Npcs(_snap()).inside(area).all()) == [1, 3, 5]
    assert _ids(Npcs(_snap()).outside(area).all()) == [2, 4]
    assert _ids(Npcs(_snap()).on_plane(1).all()) == [4]


def test_last_random_and_nearest_to():
    # Failure: nearest_to picking the farthest, or ignoring the plane of an entity origin.
    q = Npcs(_snap()).with_ids(GOBLIN)
    assert q.last().server_index == 2
    assert q.random(random.Random(5)).server_index in (1, 2)
    assert Npcs(_snap()).nearest_to(3211, 3200).server_index == 2
    assert Npcs(_snap()).nearest_to(Coordinate(3229, 3200, 1)).server_index == 4
    assert Npcs(_snap()).nearest_to(3229, 3200, 0).server_index == 2


@dataclass
class _Def:
    name: str
    ops: tuple = ()
    ground_ops: tuple = ()


class _Source:
    def __init__(self):
        self.asked = []

    def npc(self, i):
        self.asked.append(("npc", i))
        return {GOBLIN: _Def("Goblin", ("Attack", "", "", "", "Examine")),
                COW: _Def("Cow", ("Attack",)),
                GUARD + 1: _Def("Guard", ("Attack", "Pickpocket"))}.get(i)

    def loc(self, i):
        return {7: _Def("Tree", ("Chop down",)), 8: _Def("Oak", ("Chop down",))}.get(i)

    def item(self, i):
        return {995: _Def("Coins", ground_ops=("", "", "Take"))}.get(i)


def test_names_and_options_use_the_active_type_and_skip_hidden_morphs():
    # Failure: the base type (GUARD) named instead of the morph (GUARD + 1), or a hidden NPC
    # matched.
    defs = EntityDefs(_Source())
    q = lambda: Npcs(_snap())  # noqa: E731
    assert _ids(q().named_any_of("guard", "COW", defs=defs).all()) == [3, 4]
    assert _ids(q().with_option("pickpocket", defs).all()) == [4]
    assert defs.name(npc(5, 999, 0, 0, active=-1)) is None


def test_players_match_on_their_own_name():
    snap = FakeSnapshot(player_rows=[player(9, "Zezima", 1, 1), player(10, "Bob B", 2, 2)])
    assert _ids(Players(snap).named_any_of("bob b", defs=EntityDefs(_Source())).all()) == [10]


def test_interact_by_option_name_sends_the_option_slot():
    # Failure: a 0-based slot (OBJECT0 / NPC0), or the base loc id swapped for the resolved one.
    defs = EntityDefs(_Source())
    game = FakeGame()
    tree = loc(1276, 3210, 3210, resolved=8)
    assert interact(game, tree, "Chop down", defs) is True
    assert interact(game, npc(4, GUARD, 1, 1, active=GUARD + 1), "Pickpocket", defs) is True
    assert interact(game, GroundItem(995, 10, 3, 4, 0), "take", defs) is True
    assert interact(game, npc(1, GOBLIN, 1, 1), "Talk to", defs) is False
    assert game.queued == [(3, 1276, 3210, 3210), (10, 4, 0, 0), (20, 995, 3, 4)]


class _Host:
    def __init__(self, routes):
        self.routes = routes

    def path(self, x, y, plane, **_):
        if (x, y) not in self.routes:
            raise PathError("no route")
        return []


def test_reachable_filter_asks_the_walker_per_row():
    # Failure: an unreachable NPC kept (or a reachable one dropped).
    game = FakeGame(_snap())
    walker = SceneWalker(game, _Host({(3205, 3200), (3201, 3203)}))
    assert walker.is_reachable(Tile(3205, 3200, 0))
    assert not walker.is_reachable(Coordinate(3210, 3200))
    assert _ids(Npcs(_snap()).with_ids(GOBLIN, COW).reachable(walker).all()) == [1, 3]


def test_ground_items_and_scene_objects_take_the_same_filters():
    snap = FakeSnapshot(loc_rows=[loc(7, 5, 5, anim=866), loc(8, 6, 6)],
                        ground_rows=[GroundItem(995, 100, 5, 5, 0), GroundItem(526, 1, 9, 9, 0)])
    assert [l.type_id for l in SceneObjects(snap).animating().all()] == [7]
    assert [g.item_id for g in GroundItems(snap).with_ids(526).all()] == [526]
    assert [g.item_id for g in GroundItems(snap)
            .named_any_of("coins", defs=EntityDefs(_Source())).all()] == [995]
