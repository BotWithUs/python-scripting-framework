"""The walk progress wrappers (NativeHost.walk_ex / walk_start / ..., SceneWalker) over a fake
native backend that returns dicts in the real binding's shape. No client, no worldwalker.dll.
"""
import pytest

from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import Tile
from botwithus.core import native as native_mod
from botwithus.core import walk as W
from botwithus.core.native import NativeHost, NativeUnavailable, PathError
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot, walk_event, walk_result, walk_transition

DOOR = walk_transition(kind=W.WALK_TX_OBJECT, transition_index=42, step_index=1,
                       object_id=1530, tile={"x": 3205, "y": 3206, "plane": 0},
                       option_index=1, interact_attempts=3, interact_issued=0)


def _setup(be=None):
    be = be or FakeNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)))
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return be, host, SceneWalker(game, host)


def test_constants_match_the_host_header():
    assert (W.WALK_RUNNING, W.WALK_ARRIVED, W.WALK_FAILED, W.WALK_CANCELLED,
            W.WALK_TIMED_OUT, W.WALK_ERROR) == (0, 1, 2, 3, 4, 5)
    assert (W.WALK_EV_STEP_ADVANCED, W.WALK_EV_STUCK, W.WALK_EV_REPLAN_STARTED,
            W.WALK_EV_ARRIVED, W.WALK_EV_FAILED, W.WALK_EV_NONE) == (0, 3, 4, 5, 6, -1)
    assert (W.WALK_TX_NONE, W.WALK_TX_OBJECT, W.WALK_TX_NPC, W.WALK_TX_CHAIN,
            W.WALK_TX_TELEPORT) == (0, 1, 2, 3, 4)


def test_walk_ex_decodes_a_transition_failure():
    be, host, _ = _setup()
    be.walk_progress = walk_result(status=W.WALK_FAILED, result=-7, final_event=W.WALK_EV_FAILED,
                                   fail_step=1, fail_transition=42, transition=DOOR,
                                   error="the walker gave up; see the walk's events")
    be.walk_events_list = [walk_event(W.WALK_EV_STEP_ADVANCED, 0, -1, hint=4),
                           walk_event(W.WALK_EV_STEP_ADVANCED, 1, 42, t_ms=600),
                           walk_event(W.WALK_EV_FAILED, 1, 42, t_ms=4200)]
    r = host.walk_ex(3210, 3210, 0, 2)
    assert be.walk_calls == [(3210, 3210, 0, 2)]
    assert isinstance(r, W.WalkResult) and not r.arrived and not r.is_running
    assert (r.status, r.result, r.fail_step, r.fail_transition) == (W.WALK_FAILED, -7, 1, 42)
    t = r.failed_on_transition
    assert t is not None and t.kind == W.WALK_TX_OBJECT
    assert (t.object_id, t.tile, t.option_index) == (1530, Tile(3205, 3206, 0), 1)
    assert (t.interact_attempts, t.interact_issued) == (3, 0)
    assert [e.kind for e in r.events] == [0, 0, 6]
    assert r.events[0].interaction_hint == 4 and r.events[2].t_ms == 4200


def test_failed_on_transition_is_none_for_a_walk_step_failure():
    be, host, _ = _setup()
    be.walk_progress = walk_result(status=W.WALK_FAILED, result=-7, fail_step=3,
                                   fail_transition=-1, replans=3, transition=DOOR)
    r = host.walk_ex(1, 2)
    assert r.failed_on_transition is None   # the door was an earlier, passed transition
    assert r.transition.transition_index == 42 and r.replans == 3


def test_npc_chain_steps_decode():
    be, host, _ = _setup()
    npc = walk_transition(kind=W.WALK_TX_NPC, transition_index=7, step_index=1, object_id=100,
                          npc_type_hi=105, tile={"x": 3200, "y": 3201, "plane": 0},
                          option_index=0, chain_count=2,
                          chain=[{"kind": 6, "args": [0, 3200, 3201, 0, 5, 100, 105, 0, 0]},
                                 {"kind": 2, "args": [1188, 0, 0, 0, 0, 0, 0, 0, 0]}])
    be.walk_progress = walk_result(status=W.WALK_FAILED, fail_step=1, fail_transition=7,
                                   transition=npc)
    t = host.walk_ex(1, 2).failed_on_transition
    assert (t.kind, t.object_id, t.npc_type_hi) == (W.WALK_TX_NPC, 100, 105)
    assert [c.kind for c in t.chain] == [6, 2]
    assert t.chain[0].args == (0, 3200, 3201, 0, 5, 100, 105, 0, 0)


def test_nonblocking_trio_and_cursor():
    be, _host, nav = _setup()
    be.walk_progress = walk_result(status=W.WALK_RUNNING, result=0, final_event=0)
    be.walk_events_list = [walk_event(0), walk_event(W.WALK_EV_STUCK)]
    nav.start(Tile(3210, 3205, 0), radius=2)
    assert be.walk_calls == [(3210, 3205, 0, 2)]
    evs = nav.events()
    assert [e.kind for e in evs] == [0, W.WALK_EV_STUCK]
    assert nav.events(len(evs)) == []
    assert nav.wait(600).is_running and be.walk_wait_calls == [600]
    assert nav.wait(-5) is not None and be.walk_wait_calls[-1] == 0   # clamped
    nav.cancel()
    assert be.walk_cancelled
    assert nav.status().status == W.WALK_RUNNING


@pytest.mark.parametrize("asked, sent", [
    (0, 0),
    (-1, 0),
    (0xFFFFFFFE, 0xFFFFFFFE),
    (0xFFFFFFFF, 0xFFFFFFFF),
    (0x1_0000_0000, 0xFFFFFFFF),      # would wrap to 0 (return at once) unclamped
    (0x1_0000_0258, 0xFFFFFFFF),      # would wrap to 600 ms unclamped
    (10 ** 30, 0xFFFFFFFF),
])
def test_wait_timeout_is_clamped_to_the_hosts_u32(asked, sent):
    """Issue #26: the host parses timeout_ms as an unsigned 32-bit int, which wraps. Every
    value sent is in 0..2^32-1, and a value past the top saturates instead of wrapping."""
    be, host, nav = _setup()
    be.walk_progress = walk_result(status=W.WALK_RUNNING, result=0, final_event=0)
    nav.wait(asked)
    host.walk_wait(asked)
    assert be.walk_wait_calls == [sent, sent]
    assert 0 <= be.walk_wait_calls[0] <= native_mod.WALK_WAIT_MAX_MS


def test_clamp_wait_ms_is_the_u32_range():
    assert native_mod.WALK_WAIT_MAX_MS == 2 ** 32 - 1
    assert native_mod.clamp_wait_ms(600) == 600
    assert native_mod.clamp_wait_ms(2 ** 32) == 2 ** 32 - 1


def test_clamp_wait_ms_infinity_is_wait_forever_and_nan_raises():
    """0xFFFFFFFF is the host's kWaitForever (an untimed wait until the walk ends)."""
    assert native_mod.WALK_WAIT_FOREVER == 0xFFFFFFFF
    assert native_mod.clamp_wait_ms(float("inf")) == native_mod.WALK_WAIT_FOREVER
    assert native_mod.clamp_wait_ms(float("-inf")) == 0
    assert native_mod.clamp_wait_ms(1e300) == native_mod.WALK_WAIT_FOREVER
    assert native_mod.clamp_wait_ms(600.9) == 600
    with pytest.raises(ValueError):
        native_mod.clamp_wait_ms(float("nan"))


def test_wait_forever_reaches_the_host_from_the_script_api():
    be, _host, nav = _setup()
    be.walk_progress = walk_result(status=W.WALK_RUNNING, result=0, final_event=0)
    nav.wait(float("inf"))
    assert be.walk_wait_calls == [0xFFFFFFFF]


def test_refused_start_raises_path_error():
    be, host, _ = _setup()
    be.walk_progress = None   # the fake refuses like a host with a walk running
    with pytest.raises(PathError, match="already running"):
        host.walk_start(1, 2)
    with pytest.raises(PathError):
        host.walk_ex(1, 2)


def test_never_walked_is_none():
    be, host, nav = _setup()
    be.walk_progress = None
    assert host.walk_status() is None and nav.wait(0) is None and host.walk_events() == []


class _OldHost(FakeNative):
    """A bwu_host from before walk progress: walk/walk_cancel only."""
    walk_ex = walk_start = walk_status = walk_wait = walk_events = None


def test_feature_detection_against_an_older_host():
    be, host, nav = _setup(_OldHost(snapshot=build_snapshot()))
    assert not host.has_walk_progress and not nav.has_progress
    assert nav.walk(Tile(1, 2, 0)) is True       # the bool walk still works
    with pytest.raises(NativeUnavailable, match="predates walk progress"):
        nav.walk_ex(Tile(1, 2, 0))


def test_feature_detection_against_a_current_host():
    _be, host, nav = _setup()
    assert host.has_walk_progress and nav.has_progress
