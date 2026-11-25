"""The v25 surface of THIS framework, in the real ``bwu_host``, against native's fake_agent.

The native repo's ``tools/fake_agent/check_rows.py`` proves ``botwithus._native``. It never imports
this package. This does, so the real ``snapshot_bytes`` / ``rpc_raw`` bytes reach the decoders here.

    set PYTHONPATH=<this repo>\\src
    set BWU_FAKE_PID=<fake_agent pid>
    set BWU_FAKE_EXPECT=rows|rpc|lobby|noself|zero|own0|unresolved
    bwu_host --py tests\\live\\v25_fake_agent_check.py

    rows    fake_agent --version 25
    rpc     fake_agent --version 25 --answer-rpc 1
    lobby   fake_agent --version 25 --game-state 20
    noself  fake_agent --version 25 --no-self 1
    zero    fake_agent --version 25 --zero-frame 1
    own0    fake_agent --version 25 --own-index 0
    unresolved  fake_agent --version 25 --unresolved-self 1 (native b814645 or later):
                the agent's unresolved-entity case, a valid player with tile -1. The check
                refuses any other frame instead of passing on it, --unresolved-self 2 included.

Exits non-zero on any mismatch. Developer tooling: pytest does not collect it.
"""
import os

from botwithus.api.entities import HintArrows, Players
from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import (
    NO_LOCAL_PLAYER,
    EntityReadStatus,
    Headbar,
    HintArrow,
    Tile,
)
from botwithus.core.native import NativeHost, PathError
from botwithus.core.rpc import MalformedReply, RpcClient, RpcError
from botwithus.core.snapshot import SnapshotView
from botwithus.core.variables import VariableReader

failures = []
UNKNOWN = Tile(-1, -1, -1)
TILE_ARROW = HintArrow("tree", -1, 2, "tile_centre", "tile", -1, 0, 3222, 3218, 12)
NPC_ARROW = HintArrow("slot", 3, 1, "npc", "npc", 30, 0, 3204, 3205, 5)


def expect(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_rows(game):
    snap = game.snapshot()
    me = snap.self_player
    expect("self valid", me.valid, True)
    expect("self visual tile", me.tile, Tile(3200, 3201, 1))
    expect("self server tile", me.server_tile, Tile(3203, 3204, 1))
    expect("self run energy still in place", me.run_energy, 73)
    expect("player server tiles", [p.server_tile for p in snap.players()],
           [Tile(3203, 3204, 1), UNKNOWN, Tile(3202, 3203, 0)])
    expect("player has_server_tile", [p.has_server_tile for p in snap.players()],
           [True, False, True])
    expect("player names past the v25 stride", [p.name for p in snap.players()],
           ["Sir Test", "", "ABCDEFGHIJKLMNO"])
    expect("query", Players(snap).within(Tile(3200, 3201, 1), 0).count(), 1)
    expect("npcs after the grown self block", [n.active_type_id for n in snap.npcs()], [11476, -1])
    # Raw rows: unlike _native.locations, SnapshotView.locations does not filter.
    expect("locations past the moved tail", [loc.resolved_id for loc in snap.locations()],
           [125205, 4321, 888, 778, 0])
    expect("open ifaces past the moved tail", snap.open_ifaces(), [517, 1477, 1432, 1188, 1500])


def check_lobby(game):
    me = game.snapshot().self_player
    expect("server tile out of world", me.server_tile, UNKNOWN)
    expect("has_server_tile out of world", me.has_server_tile, False)
    expect("run energy out of world", me.run_energy, -1)


def expect_raises(label, exception, fn, *args, match=""):
    try:
        got = fn(*args)
    except exception as exc:
        print(f"{'ok  ' if match in str(exc) else 'FAIL'} {label}: raised "
              f"{type(exc).__name__}: {exc}")
        if match not in str(exc):
            failures.append(f"{label}: raised {exc!r}, want {match!r}")
    else:
        failures.append(f"{label}: returned {got!r}")


def check_no_player(game, host):
    """A frame with no player, whichever shape it takes. The Python guard stops the plan and the
    walks before the host, so this does not reach the host's own refusal: native's
    tests/self_gate_tests.cpp covers that. The pipe does not answer in these modes, so a
    local_health that read varps would still end in None: only the unit tests can show it reads
    nothing."""
    snap = game.snapshot()
    me = snap.self_player
    print(f"     frame: gameState {snap.game_state} ownIndex {snap.own_index}")
    expect("self is the no-player record", me, NO_LOCAL_PLAYER)
    expect("valid", me.valid, False)
    expect("server_index", me.server_index, -1)
    expect("combat_level", me.combat_level, -1)
    expect("tile", me.tile, UNKNOWN)
    expect("server_tile", me.server_tile, UNKNOWN)
    expect("skills", me.skills, ())
    expect("facing", (me.orientation, me.facing, me.facing_degrees), (-1, None, None))
    expect("run", (me.run_energy, me.run_weight), (-1, -1))
    expect("local_health", game.local_health(), None)
    nav = SceneWalker(game, host)
    expect("walk_to does not start", nav.walk_to(Tile(3200, 3201, 1)), False)
    expect("walk_to never arrives at 0,0,0", nav.walk_to(Tile(0, 0, 0)), False)
    expect("walk reports no arrival", nav.walk(Tile(3200, 3201, 1)), False)
    goal = Tile(3200, 3201, 1)
    expect_raises("plan refused", PathError, nav.plan, goal, match="not in game")
    expect_raises("walk_ex refused", PathError, nav.walk_ex, goal, match="not in game")
    expect_raises("start refused", PathError, nav.start, goal, match="not in game")


def check_unresolved_tile(game, host):
    """A valid player with no tile. Without the frame there is nothing to prove, so stop there."""
    me = game.snapshot().self_player
    if not (me.valid and me.tile == UNKNOWN):
        raise SystemExit(f"not the unresolved frame: valid={me.valid} tile={me.tile}")
    expect("has_tile", me.has_tile, False)
    nav = SceneWalker(game, host)
    goal = Tile(3200, 3201, 1)
    expect("walk_to does not start", nav.walk_to(goal), False)
    expect("walk_to never arrives at the sentinel", nav.walk_to(UNKNOWN), False)
    expect("walk reports no arrival", nav.walk(goal), False)
    expect_raises("plan refused", PathError, nav.plan, goal, match="not in game")
    expect_raises("walk_ex refused", PathError, nav.walk_ex, goal, match="not in game")
    expect_raises("start refused", PathError, nav.start, goal, match="not in game")
    expect("nothing started on the host", nav.status(), None)


def check_own_index_zero(game):
    me = game.snapshot().self_player
    expect("valid at own index 0", me.valid, True)
    expect("server_index", me.server_index, 0)
    expect("tile", me.tile, Tile(3200, 3201, 1))
    expect("combat_level", me.combat_level, 126)
    expect("run_energy", me.run_energy, 73)
    expect("server_tile", me.server_tile, Tile(3203, 3204, 1))


def check_arrows(game):
    first = game.hint_arrows()
    expect("hint_arrows truncated on odd calls", (first.truncated, first.ok), (True, True))
    second = game.hint_arrows()
    expect("hint_arrows", second.arrows, (TILE_ARROW, NPC_ARROW))
    expect("hint_arrows not truncated on even calls", second.truncated, False)
    expect("query: nearest skips the npc arrow",
           HintArrows(game).nearest(Tile(3204, 3205, 0)).target_type, "tile")
    expect("query: within skips the npc arrow",
           HintArrows(game).within(Tile(3204, 3205, 0), 1).count(), 0)
    expect("query: still lists it", HintArrows(game).count(), 2)


def check_overhead(game):
    text = game.overhead_text("npc", 30)
    expect("overhead npc", (text.text, text.expires_in_ms, text.truncated, text.ok),
           ("Cheep!", 1800, False, True))
    text = game.overhead_text("player", 7)
    expect("overhead player", (text.text, text.truncated), ("Café ok", True))
    silent = game.overhead_text("npc", 31)
    expect("overhead silent", (silent.text, silent.ok), (None, True))


def check_headbars(game):
    bars = game.headbars("npc", 30)
    expect("headbars npc", bars.headbars, (Headbar(0, 128, 200, 5000, 30), Headbar(-1, 0, 0, 0, 0)))
    expect("headbars empty", (game.headbars("npc", 31).headbars, game.headbars("npc", 31).ok),
           ((), True))
    own = game.headbars("player", 7)
    expect("headbars self", (own.headbars[0].headbar_id, own.truncated), (7, True))


def check_refusals(game):
    # 99 not_found, 98 not_in_world, 97 channel_unsupported: the real bytes, through rpc_raw.
    for index, status in ((99, EntityReadStatus.NOT_FOUND), (98, EntityReadStatus.NOT_IN_WORLD),
                          (97, EntityReadStatus.CHANNEL_UNSUPPORTED)):
        text, bars = game.overhead_text("npc", index), game.headbars("npc", index)
        expect(f"refusal {index}", (text.status, bars.status, text.text, bars.headbars),
               (status, status, None, ()))
    expect("unsupported", game.headbars("npc", 97).unsupported, True)
    expect_raises("bad kind", ValueError, game.headbars, "loc", 30)
    expect_raises("malformed text", MalformedReply, game.overhead_text, "npc", 666)
    expect_raises("malformed headbars", MalformedReply, game.headbars, "npc", 666)
    expect_raises("host timeout is not a refusal", RpcError, game.headbars, "npc", 96)


def main():
    pid, mode = int(os.environ["BWU_FAKE_PID"]), os.environ["BWU_FAKE_EXPECT"]
    host = NativeHost.attach(pid)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()),
                   VariableReader(host))
    if mode == "rows":
        check_rows(game)
    elif mode == "lobby":
        check_lobby(game)
        check_no_player(game, host)
    elif mode in ("noself", "zero"):
        check_no_player(game, host)
    elif mode == "own0":
        check_own_index_zero(game)
    elif mode == "unresolved":
        check_unresolved_tile(game, host)
    elif mode == "rpc":
        check_arrows(game)
        check_overhead(game)
        check_headbars(game)
        check_refusals(game)
    else:
        raise SystemExit(
            "BWU_FAKE_EXPECT must be rows, rpc, lobby, noself, zero, own0 or unresolved")
    if failures:
        print("FAILED\n  " + "\n  ".join(failures))
        raise SystemExit(1)
    print(f"framework {mode} ok")


main()
