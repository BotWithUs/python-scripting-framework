"""The walk transition log (WalkResult.transitions, NativeHost.walk_transitions / walk_describe,
SceneWalker.transitions / describe) over a fake native backend in the real binding's shape,
against a host that has the calls and one that predates them.
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

from fakes import FakeNative, build_snapshot, transition_info, walk_result, walk_transition

DOOR_REC = transition_info()
NPC_REC = transition_info(
    origin=W.TX_ORIGIN_NPC, kind=W.TX_KIND_TRANSPORT, move_category=3, object_id=-1,
    npc_type_min=100, npc_type_max=105, npc_search_radius=5, option_index=0,
    origin_tile={"x": 3200, "y": 3201, "plane": 0}, dest_tile={"x": 2900, "y": 3100, "plane": 1},
    rotation=0, cost=12.0, requirement_count=2, chain_step_count=2,
    requirements=[{"kind": W.REQ_SKILL, "id": 16, "amount": 30},
                  {"kind": W.REQ_VARBIT_AT_LEAST, "id": 2000, "amount": 4}],
    chain=[{"kind": 6, "args": [0, 3200, 3201, 0, 5, 100, 105, 0, 0]},
           {"kind": 2, "args": [1188, 0, 0, 0, 0, 0, 0, 0, 0]}])
RING_REC = transition_info(origin=W.TX_ORIGIN_GLOBAL, kind=W.TX_KIND_FAIRY_RING,
                           is_runtime_loaded=True, object_id=-1, option_index=-1, rotation=0,
                           origin_tile={"x": 0, "y": 0, "plane": 0}, code="CKS")

DOOR = walk_transition(kind=W.WALK_TX_OBJECT, transition_index=42, step_index=1,
                       object_id=1530, tile={"x": 3205, "y": 3206, "plane": 0},
                       option_index=1, interact_attempts=1, interact_issued=1, baked=DOOR_REC)
NPC = walk_transition(kind=W.WALK_TX_NPC, transition_index=7, step_index=3, object_id=100,
                      npc_type_hi=105, tile={"x": 3200, "y": 3201, "plane": 0}, option_index=0,
                      interact_attempts=2, interact_issued=0, baked=NPC_REC)


def _setup(be=None):
    be = be or FakeNative(snapshot=build_snapshot(self_tile=(3200, 3200, 0)))
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return be, host, SceneWalker(game, host)


def test_constants_match_the_host_header():
    assert (W.TX_ORIGIN_LOC, W.TX_ORIGIN_NPC, W.TX_ORIGIN_GLOBAL) == (0, 1, 2)
    assert (W.TX_KIND_TRANSPORT, W.TX_KIND_FAIRY_RING, W.TX_KIND_TELEPORT_CHAIN,
            W.TX_KIND_SPELL, W.TX_KIND_LODESTONE, W.TX_KIND_ITEM_TELEPORT) == (0, 1, 2, 3, 4, 5)
    assert (W.REQ_SKILL, W.REQ_ITEM, W.REQ_VARBIT, W.REQ_VARP, W.REQ_VARBIT_AT_LEAST,
            W.REQ_VARP_AT_LEAST, W.REQ_VARP_BIT) == (0, 1, 2, 3, 4, 5, 6)
    assert (W.WALK_MAX_CHAIN, W.WALK_MAX_TRANSITIONS,
            W.TX_MAX_REQUIREMENTS, W.TX_MAX_CHAIN_STEPS) == (8, 128, 16, 16)


def test_walk_ex_keeps_every_transition_with_its_baked_record():
    be, host, _ = _setup()
    be.walk_progress = walk_result(status=W.WALK_FAILED, fail_step=3, fail_transition=7,
                                   transition=NPC)
    be.walk_tx_log = [DOOR, NPC]
    r = host.walk_ex(2900, 3100, 1)
    assert [t.transition_index for t in r.transitions] == [42, 7]
    assert r.transitions_attempted == 2 and not r.transitions_cut
    door, npc = r.transitions
    assert (door.kind, door.object_id, door.tile) == (W.WALK_TX_OBJECT, 1530, Tile(3205, 3206, 0))
    assert door.baked == W.TransitionInfo.from_native(DOOR_REC)
    assert (npc.interact_attempts, npc.interact_issued) == (2, 0)
    assert r.failed_on_transition is not None and r.failed_on_transition.baked is not None


def test_baked_record_decodes_every_field():
    be, host, _ = _setup()
    be.walk_progress = walk_result(transition=NPC)
    b = host.walk_ex(1, 2).transition.baked
    assert isinstance(b, W.TransitionInfo)
    assert (b.origin, b.kind, b.move_category, b.is_runtime_loaded) == (
        W.TX_ORIGIN_NPC, W.TX_KIND_TRANSPORT, 3, False)
    assert (b.origin_tile, b.dest_tile) == (Tile(3200, 3201, 0), Tile(2900, 3100, 1))
    assert (b.object_id, b.npc_type_min, b.npc_type_max, b.npc_search_radius) == (-1, 100, 105, 5)
    assert (b.option_index, b.shape, b.rotation, b.code, b.cost) == (0, 0, 0, "", 12.0)
    assert b.requirements == (W.Requirement(W.REQ_SKILL, 16, 30),
                              W.Requirement(W.REQ_VARBIT_AT_LEAST, 2000, 4))
    assert [c.kind for c in b.chain] == [6, 2]
    assert b.chain[0].args == (0, 3200, 3201, 0, 5, 100, 105, 0, 0)
    assert not b.requirements_cut and not b.chain_cut


def test_cut_lists_are_reported():
    be, host, _ = _setup()
    be.walk_progress = walk_result()
    be.walk_tx_log = [DOOR]
    be.walk_tx_attempted = 130
    assert host.walk_ex(1, 2).transitions_cut
    cut = W.TransitionInfo.from_native(transition_info(requirement_count=20, chain_step_count=1))
    assert cut.requirements_cut and cut.chain_cut


def test_transition_with_no_baked_record_is_none():
    be, host, _ = _setup()
    be.walk_progress = walk_result()   # default transition: index -1, baked None
    assert host.walk_ex(1, 2).transition.baked is None


def test_walk_transitions_with_cursor():
    be, host, nav = _setup()
    be.walk_tx_log = [DOOR, NPC]
    assert [t.transition_index for t in host.walk_transitions()] == [42, 7]
    assert [t.transition_index for t in nav.transitions(1)] == [7]
    assert nav.transitions(2) == []
    assert len(host.walk_transitions(-3)) == 2   # clamped, never passed through to raise


def test_walk_transitions_never_walked_is_empty():
    _be, host, _ = _setup()
    assert host.walk_transitions() == []


def test_walk_describe():
    be, host, nav = _setup()
    be.walk_baked = {42: DOOR_REC, 99: RING_REC}
    assert host.walk_describe(42) == W.TransitionInfo.from_native(DOOR_REC)
    ring = nav.describe(99)
    assert (ring.origin, ring.kind, ring.code, ring.is_runtime_loaded) == (
        W.TX_ORIGIN_GLOBAL, W.TX_KIND_FAIRY_RING, "CKS", True)
    assert host.walk_describe(-1) is None and host.walk_describe(5) is None
    assert be.walk_describe_calls == [42, 99, -1, 5]


def test_walk_describe_without_a_walker_raises_path_error():
    be, _host, nav = _setup()
    be.walk_describe_error = "this worldwalker.dll has no ww_transition_describe"
    with pytest.raises(PathError, match="ww_transition_describe"):
        nav.describe(42)


def test_feature_detection_against_a_current_host():
    _be, host, nav = _setup()
    assert host.has_walk_transitions and nav.has_transitions


class _ProgressOnlyHost(FakeNative):
    """A bwu_host with walk progress but from before the transition log: walk_ex returns no
    transitions / transitions_attempted, and its transition dicts carry no "baked"."""
    walk_transitions = walk_describe = None

    def walk_ex(self, handle, x, y, plane, radius):
        d = super().walk_ex(handle, x, y, plane, radius)
        del d["transitions"], d["transitions_attempted"]
        d["transition"] = {k: v for k, v in d["transition"].items() if k != "baked"}
        return d


def test_feature_detection_against_an_older_host():
    be, host, nav = _setup(_ProgressOnlyHost(snapshot=build_snapshot()))
    be.walk_progress = walk_result(status=W.WALK_FAILED, fail_transition=42, transition=DOOR)
    assert host.has_walk_progress and not host.has_walk_transitions and not nav.has_transitions
    r = nav.walk_ex(Tile(1, 2, 0))                 # walk_ex still decodes
    assert r.transitions == () and r.transitions_attempted == 0
    assert r.failed_on_transition.object_id == 1530 and r.transition.baked is None
    with pytest.raises(NativeUnavailable, match="predates the walk transition log"):
        nav.transitions()
    with pytest.raises(NativeUnavailable, match="predates the walk transition log"):
        host.walk_describe(42)
