"""LocalPlayer on frames with no player, built byte by byte.

These frames are brief or need a failure to reach on a live client, so the bytes are the proof.
A frame holds a player only when ``gameState == 30 and ownIndex >= 0``; the offsets are spelled
out here so a wrong entry in ``layout`` cannot agree with itself. Both agent shapes are covered,
because an older v25 agent can still be attached: before NXTLibrary #58 (3c4db5e) the self block
is zeroed and a never-published frame is all zeros; from it on, ``serverIndex`` and the server tile
read -1 and a never-published frame has ``ownIndex`` -1.
"""
import importlib.util
import struct
from pathlib import Path

import pytest

from botwithus.api.game import VARP_CURRENT_HEALTH, VARP_MAX_HEALTH, GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.orientation import Direction
from botwithus.api.script import ScriptContext
from botwithus.api.snapshot_types import NO_LOCAL_PLAYER, LocalPlayer, SkillEntry, Tile
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost, PathError
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView
from botwithus.core.variables import VariableReader

from fakes import FakeNative, var_record, walk_result

SNAPSHOT_SIZE = 469_192
STATE_NONE, STATE_LOBBY, STATE_WORLD = 0, 20, 30
GAME_STATE, ROOT_IFACE, SERVER_TICK, SELF = 8, 16, 20, 24
FACING_UNKNOWN = 0xFFFF
TICK = 100
EXAMPLE_SCRIPT = Path(__file__).parent.parent / "examples" / "example_script" / "example_script.py"


def _frame(state, own_index):
    buf = bytearray(SNAPSHOT_SIZE)
    struct.pack_into("<iii", buf, GAME_STATE, state, own_index, 0)
    struct.pack_into("<i", buf, SERVER_TICK, TICK)
    return buf


def _pack_self(buf, *, server_index, tile, combat=0, moving=0, following=0, animation=0, stance=0,
               target=(0, 0), member=0, spot_anim=0, orientation=FACING_UNKNOWN, skills=(),
               run=(0, 0), server_tile=(0, 0, 0)):
    """Lay out the 568-byte self block at its v25 offsets."""
    struct.pack_into("<iihhbBhiihbBiH", buf, SELF, server_index, combat, tile[0], tile[1], tile[2],
                     moving, following, animation, stance, target[0], target[1], member,
                     spot_anim, orientation)
    struct.pack_into("<I", buf, SELF + 36, len(skills))
    for i, skill in enumerate(skills):
        struct.pack_into("<iiii", buf, SELF + 40 + i * 16, *skill)
    struct.pack_into("<ii", buf, SELF + 552, *run)
    struct.pack_into("<hhb", buf, SELF + 560, *server_tile)


WIRE_SKILL = (8, 1_000_000, 80, 82)    # type, experience, actual, boosted


def _populate_self(buf, server_index):
    """Every field holding a plausible value, none equal to the 0 / -1 a reset block holds."""
    _pack_self(buf, server_index=server_index, combat=126, tile=(3200, 3201, 1), moving=1,
               following=5, animation=808, stance=1234, target=(33, 1), member=1, spot_anim=90,
               orientation=4096, skills=[WIRE_SKILL], run=(73, 12), server_tile=(3203, 3204, 1))


# --- frame shapes -----------------------------------------------------------------------------

def _all_zero():
    """An older v25 agent's never-published frame: demand-zero. gameState 0, ownIndex 0."""
    return bytes(SNAPSHOT_SIZE)


def _reset_block(state):
    """An older v25 agent's ResetWorldBlock: ownIndex -1, zeros, the unknown facing."""
    buf = _frame(state, -1)
    _pack_self(buf, server_index=0, tile=(0, 0, 0))
    return bytes(buf)


def _post_58_block(state):
    """ResetWorldBlock from NXTLibrary #58 on: serverIndex and the server tile -1 too."""
    buf = _frame(state, -1)
    _pack_self(buf, server_index=-1, tile=(0, 0, 0), server_tile=(-1, -1, -1))
    return bytes(buf)


def _post_58_no_client():
    """ResetToNoClientFrame from NXTLibrary #58 on, also its frame before the first publish."""
    buf = bytearray(_post_58_block(STATE_NONE))
    struct.pack_into("<ii", buf, ROOT_IFACE, -1, -1)      # rootIfaceId, serverTick
    return bytes(buf)


def _stale_block(state, own_index):
    """Not a frame any agent publishes. A full self block behind a failed gate proves every
    field goes through the gate, rather than happening to read right off zeros."""
    buf = _frame(state, own_index)
    _populate_self(buf, server_index=max(own_index, 7))
    return bytes(buf)


NO_PLAYER_FRAMES = {
    "all_zero": _all_zero(),
    "state30_reset": _reset_block(STATE_WORLD),
    "lobby_reset": _reset_block(STATE_LOBBY),
    "post58_lobby": _post_58_block(STATE_LOBBY),
    "post58_state30": _post_58_block(STATE_WORLD),
    "post58_no_client": _post_58_no_client(),
    "stale_state30_own_minus1": _stale_block(STATE_WORLD, -1),
    "stale_lobby_own_7": _stale_block(STATE_LOBBY, 7),
    "stale_state0_own_0": _stale_block(STATE_NONE, 0),
}

UNKNOWN_FIELDS = {
    "valid": False,
    "server_index": -1,
    "combat_level": -1,
    "tile_x": -1,
    "tile_y": -1,
    "plane": -1,
    "tile": Tile(-1, -1, -1),
    "moving": False,
    "following_index": -1,
    "animation_id": -1,
    "stance_id": -1,
    "target_index": -1,
    "target_type": -1,
    "is_member": False,
    "spot_anim_id": -1,
    "skills": (),
    "orientation": -1,
    "facing": None,
    "facing_degrees": None,
    "run_energy": -1,
    "run_weight": -1,
    "server_tile_x": -1,
    "server_tile_y": -1,
    "server_plane": -1,
    "server_tile": Tile(-1, -1, -1),
    "has_server_tile": False,
}

no_player_frames = pytest.mark.parametrize("frame", NO_PLAYER_FRAMES.values(),
                                           ids=NO_PLAYER_FRAMES.keys())
unknown_fields = pytest.mark.parametrize("field,unknown", UNKNOWN_FIELDS.items(),
                                         ids=UNKNOWN_FIELDS.keys())


def _world_frame(own_index, *, moving=1):
    buf = _frame(STATE_WORLD, own_index)
    _populate_self(buf, server_index=own_index)
    struct.pack_into("<B", buf, SELF + 13, moving)
    return bytes(buf)


def _unresolved_self(tile=(-1, -1, -1)):
    """A real player in the world whose scene entity has not resolved: the agent
    writes all three tile fields -1."""
    buf = _frame(STATE_WORLD, 7)
    _pack_self(buf, server_index=7, combat=0, tile=tile, following=-1, animation=-1,
               stance=-1, target=(33, 1), member=1, spot_anim=-1, skills=[WIRE_SKILL],
               run=(73, 12), server_tile=(-1, -1, -1))
    return bytes(buf)


def _unlinked_self():
    """A real player whose scene entity has no graph node: the entity resolves,
    but x and y read -1 and the plane still reads, so nothing else about the entity is lost.
    Orientation is unknown because x is -1. The server tile comes from the logical position, not
    the graph node, so it can stay real. Idle, because walk_to returns False on a moving player
    whatever its guard says."""
    buf = _frame(STATE_WORLD, 7)
    _pack_self(buf, server_index=7, combat=126, tile=(-1, -1, 1), moving=0, following=5,
               animation=808, stance=1234, target=(33, 1), member=1, spot_anim=-1,
               skills=[WIRE_SKILL], run=(73, 12), server_tile=(3203, 3204, 1))
    return bytes(buf)


def _server_tile_unread():
    """A real visual tile with the server tile -1: every frame on the beta channel, and any frame
    whose server tile fails the agent's verification."""
    buf = bytearray(_world_frame(7, moving=0))
    struct.pack_into("<hhb", buf, SELF + 560, -1, -1, -1)
    return bytes(buf)


# all_minus1 and unlinked are what the agent publishes. It writes x and y together, so x_only and
# y_only are not agent output. They and plane_only pin the rule: any one -1 is no tile.
UNRESOLVED_FRAMES = {
    "all_minus1": _unresolved_self(),
    "unlinked": _unlinked_self(),
    "x_only": _unresolved_self((-1, 3201, 0)),
    "y_only": _unresolved_self((3200, -1, 0)),
    "plane_only": _unresolved_self((3200, 3201, -1)),
}

unresolved_frames = pytest.mark.parametrize("frame", UNRESOLVED_FRAMES.values(),
                                            ids=UNRESOLVED_FRAMES.keys())
refusing_calls = pytest.mark.parametrize("call", ["plan", "walk_ex", "start"])


def _example_script_loop(frame):
    """One on_loop of the shipped example on this frame. Its navigation is None, and a tile read
    past the gate would reach a Ctx with no game to sleep on, so only the gate can return."""
    spec = importlib.util.spec_from_file_location("example_script_under_test", EXAMPLE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Ctx:
        navigation = None

        @staticmethod
        def snapshot():
            return SnapshotView(frame)

    script = module.Example()
    script.on_start(Ctx())
    return script.on_loop()


class _Calls:
    """Every round trip a call makes, so a gate can show it made none."""

    def __init__(self):
        self.rpc = []
        self.paths = []
        self.walks = []
        self.varps = []

    def to_host(self):
        return (self.paths, self.walks, self.rpc)

    def responder(self, method, params):
        self.rpc.append(method)
        return {}

    def var_reads(self, fn, ids):
        self.varps.append(list(ids))
        return [var_record(VARP_CURRENT_HEALTH, FakeNative.VARP_SET, 3210,
                           kind=FakeNative.VAR_KIND_INT),
                var_record(VARP_MAX_HEALTH, FakeNative.VARP_SET, 9900,
                           kind=FakeNative.VAR_KIND_INT)]


class _RecordingNative(FakeNative):
    def __init__(self, calls, snapshot):
        super().__init__(responder=calls.responder, var_reads=calls.var_reads, snapshot=snapshot,
                         path=[{"kind": 0, "plane": 0, "x": 3205, "y": 3203}])
        self._calls = calls
        self.walk_progress = walk_result()

    def path(self, handle, x, y, plane):
        self._calls.paths.append((x, y, plane))
        return super().path(handle, x, y, plane)

    def walk(self, handle, x, y, plane, radius):
        self._calls.walks.append("walk")
        return super().walk(handle, x, y, plane, radius)

    def walk_ex(self, handle, x, y, plane, radius):
        self._calls.walks.append("walk_ex")
        return super().walk_ex(handle, x, y, plane, radius)

    def walk_start(self, handle, x, y, plane, radius):
        self._calls.walks.append("walk_start")
        return super().walk_start(handle, x, y, plane, radius)


def _setup(*frames):
    """One frame serves every read; several are served in order, one per read."""
    calls = _Calls()
    native_mod.set_backend(_RecordingNative(calls, frames[0]))
    host = NativeHost.attach(4242)
    supply = iter(frames)
    read = (lambda: SnapshotView(next(supply))) if len(frames) > 1 else (
        lambda: SnapshotView(host.snapshot_bytes()))
    game = GameApi(RpcClient(host), read, VariableReader(host))
    return calls, game, SceneWalker(game, host)


# --- no player: every field --------------------------------------------------------------------

@no_player_frames
@unknown_fields
def test_field_reads_unknown_without_a_player(frame, field, unknown):
    me = SnapshotView(frame).self_player
    assert getattr(me, field) == unknown


# --- no player: every API that assumes one -----------------------------------------------------

@no_player_frames
def test_script_context_self_player_is_not_valid(frame):
    _calls, game, _nav = _setup(frame)
    ctx = ScriptContext("presence", game, None, None)
    assert ctx.self_player.valid is False


@no_player_frames
def test_local_health_is_unknown_with_no_varp_read(frame):
    calls, game, _nav = _setup(frame)
    assert game.local_health() is None
    assert calls.varps == []


@no_player_frames
def test_local_health_does_not_serve_a_reading_cached_on_this_tick(frame):
    calls, game, _nav = _setup(_world_frame(7), frame)
    assert game.local_health() == (3210, 9900)
    assert game.local_health() is None
    assert len(calls.varps) == 1


@no_player_frames
def test_walk_to_plans_and_queues_nothing(frame):
    calls, _game, nav = _setup(frame)
    assert nav.walk_to(Tile(3210, 3205, 0)) is False
    assert (calls.paths, calls.rpc) == ([], [])


@no_player_frames
def test_walk_to_never_arrives_at_the_tile_a_zeroed_block_reads_as(frame):
    calls, _game, nav = _setup(frame)
    assert nav.walk_to(Tile(0, 0, 0), radius=1) is False
    assert (calls.paths, calls.rpc) == ([], [])


@no_player_frames
def test_example_script_waits_without_reading_a_tile(frame):
    assert _example_script_loop(frame) == 1000


@no_player_frames
def test_walk_reports_no_arrival_without_reaching_the_host(frame):
    calls, _game, nav = _setup(frame)
    assert nav.walk(Tile(3210, 3205, 0)) is False
    assert calls.to_host() == ([], [], [])


@no_player_frames
@refusing_calls
def test_refused_without_reaching_the_host(frame, call):
    calls, _game, nav = _setup(frame)
    with pytest.raises(PathError, match="not in game"):
        getattr(nav, call)(Tile(3210, 3205, 0))
    assert calls.to_host() == ([], [], [])


# The guard's frame had a tile and the one the host planned from did not, because another thread
# refreshed the host's shared frame in between. The guard cannot see that. This is the host's text
# for a lost tile from native b814645 on. An older host says "no route"; a lost player gives
# "not in game".
def test_host_refusal_after_the_guard_reaches_the_script_as_path_error():
    native_mod.set_backend(FakeNative(path="__error__:not in game: no tile yet"))
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(_world_frame(7)))
    with pytest.raises(PathError, match="not in game"):
        SceneWalker(game, host).plan(Tile(3200, 3201, 0))


# --- a player with no tile: nothing to route from -----------------------------------------------

@unresolved_frames
def test_unresolved_self_is_valid_without_a_tile(frame):
    me = SnapshotView(frame).self_player
    assert me.valid is True and me.has_tile is False


@unresolved_frames
def test_walk_to_on_unresolved_self_plans_and_queues_nothing(frame):
    calls, _game, nav = _setup(frame)
    assert nav.walk_to(Tile(3210, 3205, 0)) is False
    assert calls.to_host() == ([], [], [])


@unresolved_frames
def test_walk_to_on_unresolved_self_never_arrives_at_the_sentinel(frame):
    # Without the guard, -1 against -1 is distance 0 on the same plane: "arrived".
    calls, _game, nav = _setup(frame)
    assert nav.walk_to(Tile(-1, -1, -1), radius=1) is False
    assert calls.to_host() == ([], [], [])


@unresolved_frames
def test_walk_on_unresolved_self_reports_no_arrival(frame):
    calls, _game, nav = _setup(frame)
    assert nav.walk(Tile(3210, 3205, 0)) is False
    assert calls.to_host() == ([], [], [])


@unresolved_frames
@refusing_calls
def test_unresolved_self_is_refused_without_reaching_the_host(frame, call):
    calls, _game, nav = _setup(frame)
    with pytest.raises(PathError, match="not in game"):
        getattr(nav, call)(Tile(3210, 3205, 0))
    assert calls.to_host() == ([], [], [])


@unresolved_frames
def test_example_script_waits_for_the_tile(frame):
    assert _example_script_loop(frame) == 1000


def test_walk_to_resumes_on_the_frame_the_tile_resolves():
    calls, _game, nav = _setup(_unresolved_self(), _world_frame(7, moving=0))
    assert nav.walk_to(Tile(3210, 3205, 1)) is False
    assert calls.to_host() == ([], [], [])
    assert nav.walk_to(Tile(3210, 3205, 1)) is False
    assert calls.paths == [(3210, 3205, 1)] and calls.rpc == ["queue_action"]


@pytest.mark.parametrize("lost", [_unresolved_self(), _unlinked_self()],
                         ids=["all_minus1", "unlinked"])
def test_walk_to_stops_on_the_frame_the_tile_is_lost(lost):
    calls, _game, nav = _setup(_world_frame(7, moving=0), lost)
    goal = Tile(3210, 3205, 1)
    assert nav.walk_to(goal) is False
    assert calls.paths == [(3210, 3205, 1)] and calls.rpc == ["queue_action"]
    assert nav.walk_to(goal) is False
    assert calls.paths == [(3210, 3205, 1)] and calls.rpc == ["queue_action"]


# A script that handles one handles the other: this is the host's text from native b814645 on.
@refusing_calls
def test_guard_refuses_in_the_hosts_words(call):
    _calls, _game, nav = _setup(_unresolved_self())
    with pytest.raises(PathError) as refused:
        getattr(nav, call)(Tile(3210, 3205, 1))
    assert str(refused.value) == "not in game: no tile yet"


def test_unlinked_self_keeps_its_plane_and_entity_fields():
    me = SnapshotView(_unlinked_self()).self_player
    assert me.valid is True and me.has_tile is False
    assert me.tile == Tile(-1, -1, 1)
    assert me.has_server_tile is True and me.server_tile == Tile(3203, 3204, 1)
    assert (me.following_index, me.animation_id, me.combat_level) == (5, 808, 126)


def test_walk_to_on_unlinked_self_never_arrives_at_the_sentinel_on_its_plane():
    # Without the guard, -1,-1 against -1,-1 on the real plane is distance 0: "arrived".
    calls, _game, nav = _setup(_unlinked_self())
    assert nav.walk_to(Tile(-1, -1, 1), radius=1) is False
    assert calls.to_host() == ([], [], [])


# Each call reads a fresh frame: a guard that kept its first answer fails one of these two ways.
@pytest.mark.parametrize("first,then,reaches_host", [
    (_unresolved_self(), _world_frame(7, moving=0), (False, True)),
    (_world_frame(7, moving=0), _unresolved_self(), (True, False)),
], ids=["tile_resolves", "tile_is_lost"])
@pytest.mark.parametrize("call", ["plan", "walk", "walk_ex", "start"])
def test_guard_follows_the_frame(call, first, then, reaches_host):
    calls, _game, nav = _setup(first, then)

    def attempt():
        before = len(calls.paths) + len(calls.walks)
        try:
            getattr(nav, call)(Tile(3210, 3205, 1))
        except PathError as exc:
            assert "not in game" in str(exc)
        return len(calls.paths) + len(calls.walks) > before

    assert (attempt(), attempt()) == reaches_host


# --- a real tile with no server tile: the guard reads the visual tile only ---------------------

def test_server_tile_unread_still_has_a_tile():
    me = SnapshotView(_server_tile_unread()).self_player
    assert me.has_server_tile is False and me.server_tile == Tile(-1, -1, -1)
    assert me.has_tile is True and me.tile == Tile(3200, 3201, 1)


def test_walk_to_with_server_tile_unread_plans_and_queues():
    calls, _game, nav = _setup(_server_tile_unread())
    assert nav.walk_to(Tile(3210, 3205, 1)) is False
    assert calls.paths == [(3210, 3205, 1)] and calls.rpc == ["queue_action"]


@pytest.mark.parametrize("call,reached", [("walk", "walk"), ("walk_ex", "walk_ex"),
                                          ("start", "walk_start")])
def test_walks_with_server_tile_unread_reach_the_host(call, reached):
    calls, _game, nav = _setup(_server_tile_unread())
    getattr(nav, call)(Tile(3210, 3205, 1))
    assert calls.walks == [reached]


def test_plan_with_server_tile_unread_reaches_the_host():
    calls, _game, nav = _setup(_server_tile_unread())
    nav.plan(Tile(3210, 3205, 1))
    assert calls.paths == [(3210, 3205, 1)]


# --- a player: nothing gated, down to the lowest index -----------------------------------------

@pytest.mark.parametrize("own_index", [0, 1, 7, 2047])
@pytest.mark.parametrize("field,want", [
    ("valid", True),
    ("combat_level", 126),
    ("tile", Tile(3200, 3201, 1)),
    ("moving", True),
    ("following_index", 5),
    ("animation_id", 808),
    ("stance_id", 1234),
    ("target_index", 33),
    ("target_type", 1),
    ("is_member", True),
    ("spot_anim_id", 90),
    ("skills", (SkillEntry(*WIRE_SKILL),)),
    ("orientation", 4096),
    ("facing", Direction.WEST),
    ("facing_degrees", 270.0),
    ("run_energy", 73),
    ("run_weight", 12),
    ("server_tile", Tile(3203, 3204, 1)),
    ("has_server_tile", True),
])
def test_in_world_field_reads_the_wire(own_index, field, want):
    me = SnapshotView(_world_frame(own_index)).self_player
    assert getattr(me, field) == want
    assert me.server_index == own_index


def test_in_world_at_own_index_0_is_valid():
    me = SnapshotView(_world_frame(0)).self_player
    assert me.valid is True and me.server_index == 0


def test_in_world_at_own_index_0_reads_health():
    calls, game, _nav = _setup(_world_frame(0))
    assert game.local_health() == (3210, 9900)
    assert len(calls.varps) == 1


def test_in_world_at_own_index_0_plans_and_queues_a_walk():
    calls, _game, nav = _setup(_world_frame(0, moving=0))
    assert nav.walk_to(Tile(3210, 3205, 1)) is False
    assert calls.paths == [(3210, 3205, 1)]
    assert calls.rpc == ["queue_action"]


@pytest.mark.parametrize("call,reached", [("walk", "walk"), ("walk_ex", "walk_ex"),
                                          ("start", "walk_start")])
def test_in_world_walks_reach_the_host(call, reached):
    calls, _game, nav = _setup(_world_frame(7, moving=0))
    getattr(nav, call)(Tile(3210, 3205, 1))
    assert calls.walks == [reached]


def test_in_world_plan_reaches_the_host():
    calls, _game, nav = _setup(_world_frame(7, moving=0))
    assert [(s.target_x, s.target_y) for s in nav.plan(Tile(3210, 3205, 1))] == [(3205, 3203)]
    assert calls.paths == [(3210, 3205, 1)]


def test_in_world_at_tile_zero_has_a_tile():
    buf = bytearray(_world_frame(7, moving=0))
    struct.pack_into("<hhb", buf, SELF + 8, 0, 0, 0)
    calls, _game, nav = _setup(bytes(buf))
    assert SnapshotView(bytes(buf)).self_player.has_tile is True
    assert nav.walk_to(Tile(3210, 3205, 0)) is False
    assert calls.paths == [(3210, 3205, 0)] and calls.rpc == ["queue_action"]


def test_in_world_arrival_needs_no_route():
    calls, _game, nav = _setup(_world_frame(7, moving=0))
    assert nav.walk_to(Tile(3200, 3201, 1), radius=1) is True
    assert calls.to_host() == ([], [], [])


def test_in_world_self_not_yet_resolved_stays_valid_with_its_own_unknowns():
    # It is not the no-player record. Combat level reads 0, not -1, and what the logged-in player
    # holds rather than the entity is still real.
    me = SnapshotView(_unresolved_self()).self_player
    assert me.valid is True and me != NO_LOCAL_PLAYER
    assert me.server_index == 7 and me.combat_level == 0
    assert me.tile == Tile(-1, -1, -1) and me.server_tile == Tile(-1, -1, -1)
    assert (me.following_index, me.animation_id, me.stance_id, me.spot_anim_id) == (-1, -1, -1, -1)
    assert me.moving is False and me.orientation == -1
    assert (me.target_index, me.target_type, me.is_member) == (33, 1, True)
    assert me.skills == (SkillEntry(*WIRE_SKILL),) and (me.run_energy, me.run_weight) == (73, 12)


def test_script_context_self_player_reads_the_player_in_the_world():
    _calls, game, _nav = _setup(_world_frame(7))
    me = ScriptContext("presence", game, None, None).self_player
    assert me.valid is True and me.server_index == 7 and me.tile == Tile(3200, 3201, 1)


def test_hand_built_local_player_is_not_valid():
    # Only the decoder can say a frame holds a player.
    me = LocalPlayer(server_index=7, combat_level=126, tile_x=3200, tile_y=3201, plane=1,
                     moving=False, following_index=-1, animation_id=-1, stance_id=-1,
                     target_index=-1, target_type=-1, is_member=False, spot_anim_id=-1)
    assert me.valid is False
    assert me.has_tile is False
