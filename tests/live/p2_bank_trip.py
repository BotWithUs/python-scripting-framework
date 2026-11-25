"""P2 live check, part 2: the Bank by name, InventoryChanged events, and a lodestone teleport.
Developer tooling: pytest does not collect it. Moves the character: run it only on a disposable one.

    set PYTHONPATH=<this repo>\\src
    set BWU_NATIVE_DIR=<a dir with NXTCache.dll, worldwalker.dll + .wwa + teleport JSONs>
    bwu_host --py tests\\live\\p2_bank_trip.py

It walks (the native walker) to BWU_TRIP_BANK "x,y,plane" (default Draynor bank, 3092,3245,0),
opens the bank with Bank.open(), deposits one backpack item by its whole name and withdraws it
again, closes the bank, teleports to the Lumbridge lodestone with LodestoneNetwork.teleport(), and
walks back to the tile it started on. Every click goes through the agent's action queue.
"""
import os
import threading

from botwithus.api import Tile
from botwithus.api.banking import TransferAmount
from botwithus.api.definitions import item_name
from botwithus.api.events import InventoryChanged
from botwithus.api.lodestones import Lodestone, LodestoneNetwork
from botwithus.api.navigation import SceneWalker
from botwithus.api.script import ScriptContext
from botwithus.api.timing import bound_context
from botwithus.core import native
from botwithus.core.connection import Connection


def say(key, value):
    print(f"SMOKE {key}={value}", flush=True)


def check(label, cond, detail=""):
    if not cond:
        raise SystemExit(f"FAIL {label}: {detail}")


def walk(walker, goal, label):
    result = walker.walk_ex(goal, 2)
    me = walker._game.snapshot().self_player
    say(f"walk_{label}", f"status={result.status} error={result.error!r} "
        f"at=({me.tile_x},{me.tile_y},{me.plane}) ms={result.elapsed_ms}")
    return result


def bank_round_trip(game, ctx, events):
    bank = game.bank()
    check("bank open queued", bank.open(), "no booth, chest or banker within 10 tiles")
    check("bank opens", ctx.delay_until(bank.is_open, 15_000))
    say("bank_open", True)
    pack = [i for i in game.backpack().view().occupied()]
    check("something to deposit", bool(pack))
    target = pack[-1]
    name = item_name(target.item_id)
    before = game.backpack().view().count(target.item_id)
    say("bank_target", (target.slot, target.item_id, name, before))
    events.clear()
    check("deposit by name queued", bank.deposit(name, TransferAmount.ALL))
    check("deposited", ctx.delay_until(
        lambda: game.backpack().view().count(target.item_id) == 0, 6_000))
    ctx.delay_ticks(2)
    say("bank_deposit_events", [(e.inv_id, e.slot, e.old_item.item_id, e.new_item.item_id)
                                for e in events if isinstance(e, InventoryChanged)][:10])
    events.clear()
    check("withdraw by name queued", bank.withdraw(name, TransferAmount.ALL))
    check("withdrawn", ctx.delay_until(
        lambda: game.backpack().view().count(target.item_id) == before, 6_000))
    ctx.delay_ticks(2)
    say("bank_withdraw_events", [(e.inv_id, e.slot, e.old_item.item_id, e.new_item.item_id)
                                 for e in events if isinstance(e, InventoryChanged)][:10])
    say("bank_round_trip", f"{name} x{before} out and back")
    bank.close()
    ctx.delay_until(lambda: not bank.is_open(), 5_000)


def main():
    pid = int(os.environ.get("BWU_SMOKE_PID") or 0) or native.discover_pids()[0]
    raw = os.environ.get("BWU_TRIP_BANK", "3092,3245,0")
    bank_tile = Tile(*(int(v) for v in raw.split(",")))
    conn = Connection(pid)
    events = []
    conn.events.subscribe(InventoryChanged, events.append)
    game = conn.game
    ctx = ScriptContext("p2-bank", game, None, threading.Event())
    walker = SceneWalker(game, conn._host)
    try:
        with bound_context(ctx):
            me = game.snapshot().self_player
            start = Tile(me.tile_x, me.tile_y, me.plane)
            say("start", start)
            walk(walker, bank_tile, "to_bank")
            bank_round_trip(game, ctx, events)
            net = LodestoneNetwork(game, ctx)
            result = net.teleport(Lodestone.LUMBRIDGE)
            me = game.snapshot().self_player
            say("lodestone_lumbridge", f"{result.name} at=({me.tile_x},{me.tile_y},{me.plane}) "
                f"anim={me.animation_id}")
            walk(walker, start, "home")
    finally:
        conn.close()
    say("result", "ok")


main()
