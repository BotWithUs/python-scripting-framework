"""Walk plan options (disabled_moves / exclude / exclude_loc_siblings) on NativeHost.path / walk /
walk_ex / walk_start and their SceneWalker equivalents, over a fake native backend in the real
binding's shape: a bwu_host that has the options, and one that predates them.
"""
import pytest

from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import Tile
from botwithus.core import native as native_mod
from botwithus.core import walk as W
from botwithus.core.native import NativeHost, NativeUnavailable, PathError, WalkOptionsUnsupported
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot, walk_result

CANNOT_EXCLUDE = ("this worldwalker.dll has no ww_executor_run_opts / ww_query_opts: cannot"
                  " exclude transitions; update worldwalker.dll")
OPTION_KEYS = {"disabled_moves", "exclude", "exclude_loc_siblings"}


class OptionsNative(FakeNative):
    """A bwu_host with walk options. Records the keywords each call got; ``can_exclude``
    plays the installed worldwalker.dll, refusing a non-empty exclude the way the host does."""

    can_exclude = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.option_calls: list[tuple[str, dict]] = []

    def _take(self, name, opts):
        assert set(opts) <= OPTION_KEYS, opts
        self.option_calls.append((name, dict(opts)))
        if opts.get("exclude") and not self.can_exclude:
            raise RuntimeError(CANNOT_EXCLUDE)

    def walk_options_supported(self, handle):
        return self.can_exclude

    def path(self, handle, x, y, plane=0, **opts):
        self._take("path", opts)
        return super().path(handle, x, y, plane)

    def walk(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk", opts)
        return super().walk(handle, x, y, plane, radius)

    def walk_ex(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk_ex", opts)
        return super().walk_ex(handle, x, y, plane, radius)

    def walk_start(self, handle, x, y, plane=0, radius=1, **opts):
        self._take("walk_start", opts)
        return super().walk_start(handle, x, y, plane, radius)


def _setup(be):
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return host, SceneWalker(game, host)


def _new(**kw):
    be = OptionsNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)),
                       path=[{"kind": 0, "plane": 0, "x": 3201, "y": 3200}], **kw)
    be.walk_progress = walk_result()
    return be


def _old():
    be = FakeNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)),
                    path=[{"kind": 0, "plane": 0, "x": 3201, "y": 3200}])
    be.walk_progress = walk_result()
    return be


def test_constants_match_the_host_header():
    assert (W.MOVE_DOORS, W.MOVE_PLANE, W.MOVE_TELEPORTS, W.MOVE_CHARTERS,
            W.MOVE_OTHER_CHAINS) == (0, 2, 5, 10, 12)
    assert W.RESTRICT_FREE_TO_PLAY == 0x80000000
    assert W.WALK_MAX_EXCLUDED == 256
    assert W.moves_mask(W.MOVE_DOORS, W.MOVE_CHARTERS) == 0x401
    assert W.moves_mask() == 0
    with pytest.raises(ValueError):
        W.moves_mask(13)


def test_options_pass_through_on_every_call():
    be = _new()
    host, nav = _setup(be)
    opts = dict(disabled_moves=W.moves_mask(W.MOVE_CHARTERS), exclude=[42, 7],
                exclude_loc_siblings=True)
    host.path(1, 2, 0, **opts)
    host.walk(1, 2, 0, 1, **opts)
    host.walk_ex(1, 2, 0, 1, **opts)
    host.walk_start(1, 2, 0, 1, **opts)
    want = {"disabled_moves": 0x400, "exclude": [42, 7], "exclude_loc_siblings": True}
    assert be.option_calls == [("path", want), ("walk", want), ("walk_ex", want),
                               ("walk_start", want)]


def test_scene_walker_passes_options_through():
    be = _new()
    _, nav = _setup(be)
    goal = Tile(3300, 3300, 0)
    nav.plan(goal, exclude=(5,))
    nav.walk(goal, exclude=[5])
    nav.walk_ex(goal, 2, exclude=[5], exclude_loc_siblings=True)
    nav.start(goal, disabled_moves=W.RESTRICT_FREE_TO_PLAY)
    nav.walk_to(goal, exclude=[5])
    assert be.option_calls == [
        ("path", {"exclude": [5]}), ("walk", {"exclude": [5]}),
        ("walk_ex", {"exclude": [5], "exclude_loc_siblings": True}),
        ("walk_start", {"disabled_moves": 0x80000000}), ("path", {"exclude": [5]})]
    assert nav.has_options and nav.options_supported()


def test_default_options_keep_the_old_call_shape():
    be = _new()
    host, nav = _setup(be)
    host.path(1, 2)
    host.walk_ex(1, 2, exclude=[], disabled_moves=0, exclude_loc_siblings=False)
    nav.walk_ex(Tile(1, 2, 0), exclude=None)
    assert [opts for _, opts in be.option_calls] == [{}, {}, {}]


def test_old_bwu_host_still_walks_without_options():
    be = _old()
    host, nav = _setup(be)
    assert not host.has_walk_options and not nav.has_options
    assert host.walk_options_supported() is False
    assert host.walk_ex(1, 2, exclude=[]).status == W.WALK_ARRIVED
    assert len(nav.plan(Tile(1, 2, 0))) == 1


@pytest.mark.parametrize("call", ["path", "walk", "walk_ex", "walk_start"])
def test_old_bwu_host_refuses_options_loudly(call):
    host, _ = _setup(_old())
    with pytest.raises(NativeUnavailable, match="predates them"):
        getattr(host, call)(1, 2, exclude=[42])
    with pytest.raises(NativeUnavailable):
        getattr(host, call)(1, 2, disabled_moves=1)


@pytest.mark.parametrize("call", ["path", "walk", "walk_ex", "walk_start"])
def test_old_worldwalker_refusal_is_its_own_error(call):
    be = _new()
    be.can_exclude = False
    host, _ = _setup(be)
    assert host.walk_options_supported() is False
    with pytest.raises(WalkOptionsUnsupported, match="cannot exclude transitions") as info:
        getattr(host, call)(1, 2, exclude=[42])
    assert isinstance(info.value, PathError)
    getattr(host, call)(1, 2, exclude=[])   # an empty exclude still goes through


def test_binding_argument_errors_are_not_path_errors():
    be = _new()
    host, _ = _setup(be)

    def bad(*_a, **_k):
        raise ValueError("exclude: -1 is not a transition index (0..2147483647)")

    be.path = bad
    with pytest.raises(ValueError):
        host.path(1, 2, exclude=[-1])


def test_other_walk_failures_stay_path_errors():
    be = _new()
    be.walk_progress = None   # the fake's "a walk is already running"
    host, _ = _setup(be)
    with pytest.raises(PathError) as info:
        host.walk_ex(1, 2, exclude=[42])
    assert not isinstance(info.value, WalkOptionsUnsupported)
