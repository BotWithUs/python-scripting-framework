"""P2 live smoke: the J1-J3 composition APIs against a real injected agent and a native host at
ABI 7. Developer tooling: pytest does not collect it. Run it only on a disposable character.

    set PYTHONPATH=<this repo>\\src
    set BWU_NATIVE_DIR=<a dir with NXTCache.dll, worldwalker.dll + .wwa + teleport JSONs>
    bwu_host --py tests\\live\\p2_smoke.py

Environment: BWU_SMOKE_PID (default: the first client with an agent); BWU_SMOKE_BANK=1 to try the
bank by name (only when a bank is within reach of a single click: no walking).

What it does to the character: opens the prayer window, turns one prayer on and off, opens the
equipment tab and the backpack tab again, and (with BWU_SMOKE_BANK=1) opens a bank, deposits one
item by name and withdraws it again. Every action goes through the agent's action queue. It reads
action-bar slot 1 and leaves it alone. It never logs out.

Prints one ``SMOKE key=value`` line per fact and exits non-zero on the first hard mismatch.
"""
import os
import threading
import time

from botwithus.api import (CircularArea, Coordinate, HudTab, Npcs, Prayer, RectangularArea,
                           SceneObjects, Skill, XpTracker)
from botwithus.api.action_bar import AbilitySlot
from botwithus.api.events import InventoryChanged, ServerTick, SkillChanged
from botwithus.api.interact import EntityDefs
from botwithus.api.script import ScriptContext
from botwithus.api.timing import bound_context
from botwithus.core import native
from botwithus.core.connection import Connection

ACTION_TICKS = 6


def say(key, value):
    print(f"SMOKE {key}={value}", flush=True)


def check(label, cond, detail=""):
    if not cond:
        raise SystemExit(f"FAIL {label}: {detail}")


def wait_tick(ctx, ticks=1):
    check("ticks advance", ctx.delay_ticks(ticks), "no server tick")


def waits(ctx, game):
    start = game.snapshot().server_tick
    t0 = time.monotonic()
    met = ctx.delay_until(lambda: game.snapshot().server_tick >= start + 3, 5_000)
    took = time.monotonic() - t0
    say("wait_until_3_ticks", f"met={met} took_s={took:.2f} ticks={game.snapshot().server_tick - start}")
    check("delay_until met within 5 s", met)
    t0 = time.monotonic()
    timed_out = ctx.delay_until(lambda: False, 400)
    say("wait_timeout_400ms", f"result={timed_out} took_s={time.monotonic() - t0:.2f}")
    check("a false condition times out", not timed_out)
    stop = threading.Event()
    stopper = ScriptContext("stopper", game, None, stop)
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    stopped = stopper.delay_until(lambda: False, 30_000)
    say("wait_stopped_after_300ms", f"result={stopped} took_s={time.monotonic() - t0:.2f}")
    check("a stop ends the wait promptly", not stopped and time.monotonic() - t0 < 1.0)


def queries(game):
    snap = game.snapshot()
    me = snap.self_player
    near = RectangularArea.around(me, 15)
    defs = EntityDefs()
    inside = Npcs(snap).inside(near).all()
    say("me", (me.tile_x, me.tile_y, me.plane))
    say("npcs_total", len(snap.npcs()))
    say("npcs_in_15_box", [(n.server_index, defs.name(n), n.tile_x, n.tile_y) for n in inside][:12])
    outside = Npcs(snap).outside(near).count()
    check("inside + outside = all", len(inside) + outside == len(snap.npcs()))
    circle = CircularArea(Coordinate(me.tile_x, me.tile_y, me.plane), 6)
    say("locs_in_circle_6_with_options", [(defs.name(l), defs.options(l)) for l in
                                          SceneObjects(snap).inside(circle).all()
                                          if defs.options(l)][:8])
    attackable = Npcs(snap).inside(near).with_option("Attack", defs).not_animating().all()
    say("attackable_idle_in_box", [(n.server_index, defs.name(n)) for n in attackable][:8])


def xp(game, ctx):
    tracker = XpTracker(game, Skill.CONSTITUTION)
    wait_tick(ctx)
    me = game.snapshot().self_player
    entry = next(s for s in me.skills if s.type_id == int(Skill.CONSTITUTION))
    say("xp_constitution", f"xp={entry.experience} level={entry.actual_level} "
        f"virtual={Skill.CONSTITUTION.xp_table.level_for_xp(entry.experience)} "
        f"to_next={Skill.CONSTITUTION.xp_table.xp_to_next_level(entry.experience)}")
    say("xp_tracker", f"gained={tracker.xp_gained()} per_hour={tracker.xp_per_hour()} "
        f"elapsed_ms={tracker.elapsed_ms()}")
    check("table level matches the game", Skill.CONSTITUTION.xp_table.level_for_xp(
        entry.experience, 99) == entry.actual_level, entry)


def quests(game):
    log = game.quests()
    for quest_id in (6, 257, 132, 258, 1):
        s = log.status(quest_id)
        say(f"quest_{quest_id}", f"id={s.quest_id} name={s.name!r} stage={s.stage.name} "
            f"progress={s.progress} var={s.variable}")
    check("stub 6 reads its listed twin 257", log.status(6).quest_id == 257)
    say("quest_1_unmet", log.unmet_requirements(1))


def combat_reads(game):
    c = game.combat()
    say("combat", f"adrenaline={c.adrenaline()} life={c.life_points()}/{c.max_life_points()} "
        f"prayer={c.prayer_points()} summoning={c.summoning_points()} name={c.name()!r} "
        f"target={c.target()} in_combat={c.is_in_combat()}")
    bar = game.action_bar()
    say("action_bar_active", bar.active_bar())
    slot1 = bar.slot(1)
    say("action_bar_slot1", slot1)
    if isinstance(slot1, AbilitySlot):
        cd = bar.cooldown(slot1.ability)
        say("action_bar_slot1_cooldown", f"{cd} remaining_ms="
            f"{cd.remaining_ms(game.snapshot().game_cycle)}")
    gcd = bar.global_cooldown()
    say("global_cooldown", f"{gcd} span={gcd.end_cycle - gcd.start_cycle}")
    s = game.summoning()
    say("summoning", f"points={s.points()}/{s.max_points()} pouch={s.pouch_id()} "
        f"familiar={s.familiar()}")
    say("lodestones_unlocked", [l.name for l in game.lodestones().unlocked()])
    say("buffs_active", [b.name for b in game.buffs().active()])
    say("loot", f"open={game.loot().is_open()} area_loot={game.loot().is_area_loot_enabled()}")


def hud(game, ctx):
    h = game.hud()
    states = {t.name: h.is_open(t) for t in HudTab}
    say("hud_before", states)
    opened = h.open(HudTab.EQUIPMENT)
    check("equipment open queued", opened)
    check("equipment shows", ctx.delay_until(lambda: h.is_open(HudTab.EQUIPMENT), 4_000))
    say("hud_equipment_open", h.is_open(HudTab.EQUIPMENT))
    check("backpack open queued", h.open(HudTab.BACKPACK))
    check("backpack shows again", ctx.delay_until(lambda: h.is_open(HudTab.BACKPACK), 4_000))
    say("hud_after", {t.name: h.is_open(t) for t in HudTab})


def prayer(game, ctx):
    p = game.prayers()
    say("prayer_points", f"{p.points()}/{p.max_points()} book={p.active_book().name}")
    if p.points() <= 0:
        say("prayer_toggle", "skipped: no prayer points")
        return
    if not p.is_window_open():
        p.open_window()
        check("prayer window opens", ctx.delay_until(p.is_window_open, 4_000))
    target = Prayer.TIERED_DEFENCE
    info = p.info(target)
    say("prayer_info", info)
    was = p.is_active(target)
    check("toggle queued", p.toggle(target))
    on = ctx.delay_until(lambda: p.is_active(target) != was, 4_000)
    say("prayer_toggled", f"was={was} now={p.is_active(target)}")
    check("prayer state flipped", on)
    check("toggle back queued", p.toggle(target))
    back = ctx.delay_until(lambda: p.is_active(target) == was, 4_000)
    say("prayer_restored", f"now={p.is_active(target)}")
    check("prayer state restored", back)
    game.hud().open(HudTab.BACKPACK)


def bank(game, ctx):
    if os.environ.get("BWU_SMOKE_BANK") != "1":
        say("bank", "skipped (BWU_SMOKE_BANK!=1)")
        return
    b = game.bank()
    if not b.open():
        say("bank", "skipped: no booth, chest or banker within 10 tiles")
        return
    check("bank opens", ctx.delay_until(b.is_open, 10_000))
    pack = game.backpack().view().occupied()
    if not pack:
        say("bank_deposit", "skipped: empty backpack")
        return
    from botwithus.api.definitions import item_name
    target = pack[-1]
    name = item_name(target.item_id)
    say("bank_deposit_target", (target.slot, target.item_id, name))
    check("deposit by name queued", b.deposit(name))
    check("item left the backpack", ctx.delay_until(
        lambda: game.backpack().view().first(target.item_id) is None
        or game.backpack().view().count(target.item_id) < sum(
            i.quantity for i in pack if i.item_id == target.item_id), 5_000))
    check("withdraw by name queued", b.withdraw(name))
    check("item came back", ctx.delay_until(
        lambda: game.backpack().view().first(target.item_id) is not None, 5_000))
    say("bank_round_trip", "ok")
    b.close()


def main():
    pid = int(os.environ.get("BWU_SMOKE_PID") or 0) or native.discover_pids()[0]
    conn = Connection(pid)
    events = []
    conn.events.subscribe(InventoryChanged, events.append)
    conn.events.subscribe(SkillChanged, events.append)
    ticks = []
    conn.events.subscribe(ServerTick, lambda e: ticks.append(e.tick))
    game = conn.game
    ctx = ScriptContext("p2-smoke", game, None, threading.Event())
    try:
        with bound_context(ctx):
            me = game.snapshot().self_player
            check("in the world", game.snapshot().in_game and me.has_tile, me)
            waits(ctx, game)
            queries(game)
            xp(game, ctx)
            quests(game)
            combat_reads(game)
            hud(game, ctx)
            prayer(game, ctx)
            bank(game, ctx)
            wait_tick(ctx, 2)
        say("server_tick_events", len(ticks))
        say("derived_events", [(type(e).__name__, getattr(e, "slot", getattr(e, "skill_id", None)),
                                e.server_tick) for e in events][:20])
    finally:
        conn.close()
    say("result", "ok")


main()
