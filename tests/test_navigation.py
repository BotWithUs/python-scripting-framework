"""SceneWalker over the native host's path surface -- no client, no worldwalker.dll.

The fake native returns a canned ``path`` (the walk/transition steps ``bwu_path`` would),
so the walk-toward-goal logic is tested end to end without the pathfinder.
"""
import pytest

from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import Tile
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost, PathError, PathStep
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot


def _walk(x, y):
    return {"kind": 0, "plane": 0, "x": x, "y": y}


def _setup(path, responder=None):
    be = FakeNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)),
                    path=path, responder=responder)
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return host, SceneWalker(game, host)


def test_path_decodes_to_steps():
    host, _ = _setup([_walk(3205, 3203), {"kind": 1, "plane": 0, "x": 3210, "y": 3205}])
    steps = host.path(3210, 3205, 0)
    assert [type(s).__name__ for s in steps] == ["PathStep", "PathStep"]
    assert steps[0].is_walk and (steps[0].target_x, steps[0].target_y) == (3205, 3203)
    assert not steps[1].is_walk  # a transition step


def test_walk_to_queues_walk_toward_furthest_reachable_step():
    queued = {}

    def responder(method, params):
        if method == "queue_action":
            queued.update(params)
        return {}

    _host, nav = _setup([_walk(3205, 3203), _walk(3210, 3205)], responder=responder)
    arrived = nav.walk_to(Tile(3210, 3205, 0), radius=1, step_reach=12)
    assert arrived is False
    # WALK == 23, param1 == 1, then (tileX, tileY) of the furthest step within reach.
    assert (queued["action_id"], queued["param1"]) == (23, 1)
    assert (queued["param2"], queued["param3"]) == (3210, 3205)


def test_walk_to_returns_true_when_already_within_radius():
    calls = {"n": 0}

    def responder(method, params):
        calls["n"] += 1
        return {}

    _host, nav = _setup([_walk(3205, 3203)], responder=responder)
    assert nav.walk_to(Tile(3200, 3200, 0), radius=1) is True
    assert calls["n"] == 0  # arrived: no action queued


def test_no_route_raises_path_error():
    host, _ = _setup("__error__:no path")
    with pytest.raises(PathError):
        host.path(9999, 9999, 0)


def test_walk_delegates_to_native_executor():
    be = FakeNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)))
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    nav = SceneWalker(game, host)

    assert nav.walk(Tile(3092, 3243, 0), radius=2) is True  # blocking full walk
    assert be.walk_calls == [(3092, 3243, 0, 2)]            # goal + radius forwarded
    nav.cancel()
    assert be.walk_cancelled is True
