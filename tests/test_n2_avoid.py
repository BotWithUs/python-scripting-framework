"""Avoid areas (walk.Area, ``avoid=``) and route links (``path_route`` / SceneWalker.route) over a
fake ``_native`` in the real binding's shape (native ABI 7: ``avoid`` is a list of
(plane, min_x, min_y, max_x, max_y) tuples; path_route returns {"steps", "links"}).

Failure means: an area sent in the wrong field order, an ``avoid`` key sent when there are no
areas (the call must keep its pre-ABI-7 shape), the host's "cannot avoid areas" not raised as
WalkOptionsUnsupported, a link field decoded from the wrong key, or a bare tuple accepted where
its plane could be read from the wrong position.
"""
import pytest

from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import Tile
from botwithus.core import native as native_mod
from botwithus.core import walk as W
from botwithus.core.native import NativeHost, NativeUnavailable, PathError, Route, WalkOptionsUnsupported
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot, walk_result

CANNOT_AVOID = ("this worldwalker.dll predates avoid areas (no ww_path_links / ww_query_opts):"
                " cannot avoid areas; update worldwalker.dll")
CANNOT_LINK = ("this worldwalker.dll has no ww_path_links: cannot list route links; update"
               " worldwalker.dll")
STEPS = [{"kind": 1, "plane": 0, "x": 3233, "y": 3221}, {"kind": 0, "plane": 0, "x": 3240, "y": 3230},
         {"kind": 1, "plane": 0, "x": 2801, "y": 3003}]
LINKS = [
    {"step_index": 0, "transition_index": 300, "kind": W.TX_KIND_LODESTONE, "move_category": W.MOVE_LODESTONES,
     "origin": W.TX_ORIGIN_GLOBAL, "object_id": -1, "from": (-1, -1, -1), "to": (3233, 3221, 0),
     "code": "", "name": "Lumbridge lodestone"},
    {"step_index": 2, "transition_index": 301, "kind": W.TX_KIND_FAIRY_RING, "move_category": W.MOVE_FAIRY_RINGS,
     "origin": W.TX_ORIGIN_LOC, "object_id": 12128, "from": (3240, 3230, 0), "to": (2801, 3003, 0),
     "code": "CKS", "name": ""},
]


class AvoidNative(FakeNative):
    """A bwu_host with ABI 7 walk options. ``walker_knows_avoid`` plays the worldwalker.dll."""

    walker_knows_avoid = True

    def __init__(self, **kw):
        super().__init__(snapshot=build_snapshot(self_tile=(3200, 3200, 0)),
                         path=[{"kind": 0, "plane": 0, "x": 3201, "y": 3200}], **kw)
        self.walk_progress = walk_result()
        self.calls: list[tuple[str, dict]] = []

    def _take(self, name, opts):
        self.calls.append((name, dict(opts)))
        if opts.get("avoid") and not self.walker_knows_avoid:
            raise RuntimeError(CANNOT_AVOID)

    def walk_options_supported(self, handle):
        return True

    def path(self, handle, x, y, plane=0, **opts):
        self._take("path", opts)
        return super().path(handle, x, y, plane)

    def path_route(self, handle, x, y, plane=0, **opts):
        self._take("path_route", opts)
        if not self.walker_knows_avoid:
            raise RuntimeError(CANNOT_LINK)
        return {"steps": STEPS, "links": LINKS}

    def walk(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk", opts)
        return super().walk(handle, x, y, plane, radius)

    def walk_ex(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk_ex", opts)
        return super().walk_ex(handle, x, y, plane, radius)

    def walk_start(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk_start", opts)
        return super().walk_start(handle, x, y, plane, radius)


class PreAbi7Native(AvoidNative):
    """A bwu_host with walk options but no avoid areas or path_route (ABI 6)."""

    path_route = None


def _setup(be):
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return host, SceneWalker(game, host)


def test_area_constants_and_shapes():
    assert (W.AREA_ALL_PLANES, W.WALK_MAX_AVOID) == (-1, 64)
    assert W.Area(10, 20, 30, 40, 2).to_native() == (2, 10, 20, 30, 40)
    assert W.Area.box(30, 40, 10, 20).to_native() == (-1, 10, 20, 30, 40)
    assert W.Area.around(3205, 3206, 2, 0) == W.Area(3203, 3204, 3207, 3208, 0)
    area = W.Area(1, 1, 3, 3, 1)
    assert area.contains(2, 2, 1) and not area.contains(2, 2, 0) and not area.contains(4, 2, 1)
    assert W.Area(1, 1, 3, 3).contains(1, 3, 3)   # every plane
    with pytest.raises(ValueError, match="plane"):
        W.Area(0, 0, 1, 1, 4)
    with pytest.raises(ValueError, match="min <= max"):
        W.Area(5, 0, 4, 1)
    with pytest.raises(ValueError):
        W.Area.around(0, 0, -1)


def test_areas_reach_the_binding_in_its_order():
    be = AvoidNative()
    host, walker = _setup(be)
    door = W.Area(3204, 3205, 3206, 3207, 0)
    host.walk_ex(9002, 0, avoid=[door, W.Area.box(1, 2, 3, 4)])
    walker.walk(Tile(9002, 0, 0), avoid=[door])
    walker.start(Tile(9002, 0, 0), avoid=[door])
    walker.plan(Tile(9002, 0, 0), avoid=[door])
    assert [c for c in be.calls] == [
        ("walk_ex", {"avoid": [(0, 3204, 3205, 3206, 3207), (-1, 1, 2, 3, 4)]}),
        ("walk", {"avoid": [(0, 3204, 3205, 3206, 3207)]}),
        ("walk_start", {"avoid": [(0, 3204, 3205, 3206, 3207)]}),
        ("path", {"avoid": [(0, 3204, 3205, 3206, 3207)]}),
    ]


def test_no_areas_keeps_the_old_call_shape():
    be = AvoidNative()
    host, walker = _setup(be)
    host.walk_ex(9001, 0, avoid=[])
    walker.walk(Tile(9001, 0, 0), avoid=None)
    host.path(9001, 0, exclude=[7], avoid=())
    assert be.calls == [("walk_ex", {}), ("walk", {}), ("path", {"exclude": [7]})]


def test_bad_areas_never_reach_the_host():
    be = AvoidNative()
    host, _ = _setup(be)
    with pytest.raises(TypeError, match="walk.Area"):
        host.walk_ex(9001, 0, avoid=[(0, 1, 2, 3, 4)])
    with pytest.raises(ValueError, match="at most 64"):
        host.path(9001, 0, avoid=[W.Area(1, 1, 1, 1)] * 65)
    assert be.calls == []


def test_a_walker_without_avoid_areas_is_refused_loudly():
    be = AvoidNative()
    be.walker_knows_avoid = False
    host, walker = _setup(be)
    with pytest.raises(WalkOptionsUnsupported, match="cannot avoid areas"):
        host.walk_ex(9002, 0, avoid=[W.Area(1, 1, 2, 2)])
    with pytest.raises(WalkOptionsUnsupported, match="cannot avoid areas"):
        walker.plan(Tile(9002, 0, 0), avoid=[W.Area(1, 1, 2, 2)])
    with pytest.raises(WalkOptionsUnsupported, match="cannot list route links"):
        walker.route(Tile(9014, 0, 0))


def test_an_older_host_says_it_predates_avoid_areas():
    host, walker = _setup(PreAbi7Native())
    assert host.has_avoid is False
    with pytest.raises(NativeUnavailable, match="avoid areas"):
        host.walk_ex(9002, 0, avoid=[W.Area(1, 1, 2, 2)])
    with pytest.raises(NativeUnavailable, match="path_route"):
        walker.route(Tile(9014, 0, 0))
    assert host.walk_ex(9001, 0).status == W.WALK_ARRIVED   # no areas: still walks


def test_route_links_are_decoded():
    be = AvoidNative()
    host, walker = _setup(be)
    route = walker.route(Tile(9014, 0, 0), avoid=[W.Area(1, 1, 2, 2, 3)])
    assert isinstance(route, Route)
    assert [(s.kind, s.target_x, s.target_y) for s in route.steps] == [(1, 3233, 3221), (0, 3240, 3230), (1, 2801, 3003)]
    lode, ring = route.links
    assert (lode.step_index, lode.transition_index, lode.kind, lode.origin, lode.object_id, lode.name) == \
        (0, 300, W.TX_KIND_LODESTONE, W.TX_ORIGIN_GLOBAL, -1, "Lumbridge lodestone")
    assert lode.to_tile == Tile(3233, 3221, 0) and lode.is_teleport
    assert (ring.code, ring.object_id, ring.from_tile, ring.to_tile, ring.is_teleport) == \
        ("CKS", 12128, Tile(3240, 3230, 0), Tile(2801, 3003, 0), False)
    assert route.teleports == (lode,)
    assert be.calls[-1] == ("path_route", {"avoid": [(3, 1, 1, 2, 2)]})


def test_route_needs_a_start_tile():
    be = AvoidNative()
    be._snapshot = build_snapshot(game_state=10)   # logged out: no player
    _, walker = _setup(be)
    with pytest.raises(PathError, match="not in game"):
        walker.route(Tile(9014, 0, 0))
    assert be.calls == []
