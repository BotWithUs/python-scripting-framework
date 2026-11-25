"""N2 live smoke: cache definitions, world map elements, avoid areas and route links through this
framework, against a real injected agent and a native host at ABI 7. Developer tooling: pytest does
not collect it. It walks the character twice (out and back), so run it only on a disposable one.

    set PYTHONPATH=<this repo>\\src
    set BWU_NATIVE_DIR=<a dir with NXTCache.dll, worldwalker.dll + .wwa + teleport JSONs>
    bwu_host --py tests\\live\\n2_smoke.py

Environment: BWU_SMOKE_PID (default: the first client with an agent), BWU_SMOKE_SKIP_WALK=1 to plan
only, BWU_SMOKE_ROUTE_GOAL "x,y,plane" for the route-links plan (default Draynor, 3093,3244,0).

Prints one ``SMOKE key=value`` line per fact and exits non-zero on the first hard mismatch. The
avoid walk reports its sampled tiles as facts (``avoid_walk_samples_in_box``) rather than failing:
the executor's handling of avoid boxes is WorldWalker's, and a fix for it may still be open.
"""
import os
import time

from botwithus.api import worldmap
from botwithus.api.definitions import Definitions
from botwithus.api.game import GameApi
from botwithus.api.navigation import SceneWalker
from botwithus.api.snapshot_types import Tile
from botwithus.core import walk as W
from botwithus.core.native import NativeHost, PathError
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

LUMBRIDGE_LODESTONE = (3233, 3222)
LODESTONE_CATEGORY = 2613
CANDIDATE_OFFSETS = ((12, 0), (-12, 0), (0, 12), (0, -12), (10, 10), (-10, -10), (10, -10), (-10, 10))
BOX_RADIUS = 2
SAMPLE_S = 0.15
WALK_TIMEOUT_S = 90.0


def say(key, value):
    print(f"SMOKE {key}={value}", flush=True)


def check(label, cond, detail=""):
    if not cond:
        raise SystemExit(f"FAIL {label}: {detail}")


def attach():
    pid = int(os.environ.get("BWU_SMOKE_PID") or 0) or None
    host = NativeHost.attach(pid if pid else native_first_pid())
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    return host, game


def native_first_pid():
    from botwithus.core import native
    pids = native.discover_pids()
    check("a client with an agent", bool(pids), "none found")
    return pids[0]


def check_backpack(game, defs):
    items = game.backpack().view().items
    occupied = [it for it in items if not it.empty]
    say("backpack_occupied", len(occupied))
    check("an item in the backpack", bool(occupied))
    seen = set()
    for it in occupied:
        if it.item_id in seen:
            continue
        seen.add(it.item_id)
        d = defs.item(it.item_id)
        check(f"item {it.item_id} defined", d is not None)
        say(f"item_{it.item_id}", f"name={d.name!r} inventory_ops={list(d.inventory_ops)} "
                                  f"noted={d.noted} stackable={d.stackable} qty={it.quantity}")
        check(f"item {it.item_id} named", d.name != "")
    first = occupied[0]
    say("count_named_first", (defs.item(first.item_id).name,
                              game.backpack().count_named(defs.item(first.item_id).name)))


def check_lodestone():
    found = worldmap.elements(category=LODESTONE_CATEGORY, near=LUMBRIDGE_LODESTONE, radius=3)
    say("lumbridge_lodestone", [(e.area_id, e.element_id, e.x, e.y, e.plane, e.category) for e in found])
    check("Lumbridge lodestone in area 28", any(e.area_id == 28 for e in found), found)
    prif = worldmap.elements(231, category=LODESTONE_CATEGORY, near=(2208, 3361), radius=2)
    say("prifddinas_lodestone_231", [(e.element_id, e.x, e.y) for e in prif])
    ghost = worldmap.elements(category=LODESTONE_CATEGORY, near=(4576, 2665), radius=2)
    say("prifddinas_lodestone_28_drawn", [(e.element_id, e.x, e.y) for e in ghost])


def route_len(start, steps):
    total, (x, y) = 0, (start.x, start.y)
    for s in steps:
        total += max(abs(s.target_x - x), abs(s.target_y - y))
        x, y = s.target_x, s.target_y
    return total


def pick_detour(host, me):
    """A goal 12-ish tiles away whose plain route is all walking, and a box over its midpoint that
    makes the plan go round."""
    for dx, dy in CANDIDATE_OFFSETS:
        goal = Tile(me.x + dx, me.y + dy, me.plane)
        try:
            plain = host.path(goal.x, goal.y, goal.plane)
        except PathError:
            continue
        if not plain or any(not s.is_walk for s in plain):
            continue
        mx, my = me.x + dx // 2, me.y + dy // 2
        box = W.Area.around(mx, my, BOX_RADIUS, me.plane)
        try:
            around = host.path(goal.x, goal.y, goal.plane, avoid=[box])
        except PathError as exc:
            say(f"detour_{dx}_{dy}", f"no route round the box: {exc}")
            continue
        inside = [(s.target_x, s.target_y) for s in around if box.contains(s.target_x, s.target_y, s.plane)]
        if [(s.target_x, s.target_y) for s in around] != [(s.target_x, s.target_y) for s in plain] and not inside:
            return goal, box, plain, around
    return None


def avoid_walk(host, game, walker, me, picked):
    goal, box, plain, around = picked
    say("avoid_goal", (goal.x, goal.y, goal.plane))
    say("avoid_box", box)
    say("plan_plain", [(s.target_x, s.target_y) for s in plain])
    say("plan_plain_len", route_len(me, plain))
    say("plan_avoid", [(s.target_x, s.target_y) for s in around])
    say("plan_avoid_len", route_len(me, around))
    if os.environ.get("BWU_SMOKE_SKIP_WALK") == "1":
        say("avoid_walk", "skipped")
        return
    walker.start(goal, 1, avoid=[box])
    samples, inside = [], []
    deadline = time.monotonic() + WALK_TIMEOUT_S
    result = None
    while time.monotonic() < deadline:
        p = game.snapshot().self_player
        t = (p.tile_x, p.tile_y, p.plane)
        if not samples or samples[-1] != t:
            samples.append(t)
            if box.contains(*t):
                inside.append(t)
        result = host.walk_wait(int(SAMPLE_S * 1000))
        if result is not None and not result.is_running:
            break
    say("avoid_walk_status", result.status if result else None)
    say("avoid_walk_error", result.error if result else None)
    say("avoid_walk_samples", samples)
    say("avoid_walk_samples_count", len(samples))
    say("avoid_walk_samples_in_box", inside)
    end = game.snapshot().self_player
    say("avoid_walk_end", (end.tile_x, end.tile_y, end.plane))
    # Back to where it started, plainly, so the next run starts from the same place.
    back = host.walk_ex(me.x, me.y, me.plane, 1)
    say("walk_back_status", back.status)


def route_links(walker):
    raw = os.environ.get("BWU_SMOKE_ROUTE_GOAL", "3093,3244,0")
    x, y, plane = (int(v) for v in raw.split(","))
    route = walker.route(Tile(x, y, plane))
    say("route_goal", (x, y, plane))
    say("route_steps", len(route.steps))
    say("route_links", [(link.step_index, link.transition_index, link.kind, link.origin, link.object_id,
                         (link.to_tile.x, link.to_tile.y, link.to_tile.plane), link.code, link.name)
                        for link in route.links])


def main():
    host, game = attach()
    check("refresh", host.refresh())
    me = game.snapshot().self_player
    check("in the world with a tile", me.has_tile, me)
    say("abi", host.constant("ABI_VERSION"))
    say("start_tile", (me.tile_x, me.tile_y, me.plane))
    defs = Definitions()
    check("cache available", defs.available())
    check_backpack(game, defs)
    check_lodestone()
    walker = SceneWalker(game, host)
    picked = pick_detour(host, me.tile)
    check("an avoid box that makes the plan detour", picked is not None)
    avoid_walk(host, game, walker, me.tile, picked)
    route_links(walker)
    host.detach()
    say("result", "ok")


main()
