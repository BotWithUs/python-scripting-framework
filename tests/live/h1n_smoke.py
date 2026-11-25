"""H1n live smoke: the client reads of NXTLibrary 0f746b4 through this framework, against a real
injected agent. Developer tooling: pytest does not collect it. It changes world once, so run it
only on a disposable character.

    set PYTHONPATH=<this repo>\\src
    bwu_host --py tests\\live\\h1n_smoke.py

Environment: BWU_SMOKE_PID (default: the first client with an agent), BWU_SMOKE_HOP_WORLD (the
world to hop to; default: the first listed world other than the current one whose flags carry no
bit but members, and whose members bit fits the account), BWU_SMOKE_SKIP_HOP=1 to stay.

Prints one ``key=value`` line per fact and exits non-zero on the first mismatch.
"""
import os
import time

from botwithus.api.client_reads import WORLD_FLAG_MEMBERS
from botwithus.api.game import GameApi
from botwithus.core import native
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

HOP_TIMEOUT_S = 45.0
SETTLE_S = 10.0


def say(key, value):
    print(f"SMOKE {key}={value}", flush=True)


def check(label, cond, detail=""):
    if not cond:
        raise SystemExit(f"FAIL {label}: {detail}")


def project_self(game, tag):
    me = game.snapshot().self_player
    proj = game.world_to_screen([(me.tile_x, me.tile_y)])
    p = proj.points[0]
    say(f"{tag}_tile", (me.tile_x, me.tile_y, me.plane))
    say(f"{tag}_result", p.result)
    say(f"{tag}_xy", (p.x, p.y))
    say(f"{tag}_viewport", proj.viewport)
    check(f"{tag} own tile on screen", p.on_screen, p.result)


def pick_world(game, current, is_member):
    wanted = os.environ.get("BWU_SMOKE_HOP_WORLD")
    if wanted:
        return int(wanted)
    for w in game.world_list().worlds:
        only_members_bit = (w.flags & ~WORLD_FLAG_MEMBERS) == 0
        if w.id != current and only_members_bit and w.population > 0 and (is_member or not w.members):
            return w.id
    return None


def main():
    pid = int(os.environ.get("BWU_SMOKE_PID") or 0) or (native.discover_pids() or [0])[0]
    check("a client", pid != 0)
    host = NativeHost.attach(pid)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    account = game.account_info()
    say("account_name", account.get("account_name"))
    offers = game.ge_offers()
    say("ge_slots", len(offers))
    say("ge_in_use", sum(1 for o in offers if o.in_use))
    wl = game.world_list()
    current = game.current_world()
    say("world_count", len(wl.worlds))
    say("world_current", current)
    check("current world listed", wl.get(current) is not None, current)
    say("target_mode", game.target_mode())
    subs = game.dynamic_children_with_sub(1473, 5)
    say("backpack_children", len(subs))
    project_self(game, "w2s")
    if os.environ.get("BWU_SMOKE_SKIP_HOP") == "1":
        return
    target = pick_world(game, current, bool(account.get("is_member")))
    check("a world to hop to", target is not None)
    say("hop_target", target)
    result = game.hop_world(target, timeout_s=HOP_TIMEOUT_S)
    say("hop_result", result)
    check("hop arrived", result.arrived, result)
    check("current world is the target", game.current_world() == target)
    stop = time.monotonic() + SETTLE_S
    # gameState is back at 30 before the scene has loaded: world_to_screen answers "unavailable"
    # until it has (seen live, 951-4).
    while time.monotonic() < stop:
        me = game.snapshot().self_player
        if me.valid and game.world_to_screen([(me.tile_x, me.tile_y)]).points[0].result != "unavailable":
            break
        time.sleep(0.2)
    project_self(game, "w2s_after_hop")
    say("done", 1)


if __name__ == "__main__":
    main()
