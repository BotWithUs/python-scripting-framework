"""J2 lodestones (unlock, teleport with settle/retry, repeat) and the derived per-tick
InventoryChanged / SkillChanged events."""
import threading

from botwithus.api.events import EventBus, InventoryChanged, ServerTick, SkillChanged
from botwithus.api.lodestones import Lodestone, LodestoneNetwork, TeleportResult, has_arrived
from botwithus.api.snapshot_types import InventoryItem
from botwithus.core.change_detector import SnapshotChangeDetector

from fake_game import FakeGame, FakeSnapshot, inv, me, ops, skill

HOME = (1465, 34, -1)


class _Ctx:
    """A context the waits read: never stopped, never paused."""

    def __init__(self):
        self.is_paused = False

    def is_stop_requested(self):
        return False


def test_unlock_thresholds():
    # Failure: Lunar Isle unlocked by a quest value below 100, or Bandit Camp below 15.
    game = FakeGame()
    game.varbit_values.update({30: 1, 10236: 99, 9482: 15, 28: 0})
    net = LodestoneNetwork(game)
    assert net.is_unlocked(Lodestone.BURTHORPE)
    assert not net.is_unlocked(Lodestone.LUNAR_ISLE)
    assert net.is_unlocked(Lodestone.BANDIT_CAMP)
    assert not net.is_unlocked(Lodestone.AL_KHARID)
    assert set(net.unlocked()) == {Lodestone.BURTHORPE, Lodestone.BANDIT_CAMP}


def test_arrival_needs_twenty_tiles_or_a_plane_change():
    # Failure: a 19-tile walk interrupted by the cast read as arrived.
    start = me(3222, 3218)
    assert not has_arrived(start, me(3241, 3218))
    assert has_arrived(start, me(3242, 3218))
    assert has_arrived(start, me(3223, 3218, plane=1))
    assert not has_arrived(start, me(3222, 3218, plane=1))


def _teleporting_game(open_on_attempt=1):
    """Opens the network on the ``open_on_attempt``-th open press, lands at Burthorpe with a
    landing animation when the destination is clicked, and ends the animation later."""
    game = FakeGame(FakeSnapshot(self_player=me(3222, 3218)))
    game.varbit_values[30] = 1
    game.component_ops_map[HOME] = ops("Lodestone Network", "Teleport Lumbridge")
    presses = [0]

    def on_queue(g):
        action = g.queued[-1]
        if action[3] == (1465 << 16) | 34 and action[1] == 1:
            presses[0] += 1
            if presses[0] >= open_on_attempt:
                g.snap.open_ifaces = {1092}
        elif action[3] == (1092 << 16) | 12:
            g.snap.open_ifaces = set()
            g.snap.self_player = me(2899, 3545, animation=8941)
            threading.Timer(0.15, lambda: setattr(g.snap, "self_player", me(2899, 3545))).start()
    game.on_queue = on_queue
    return game, presses


def test_teleport_opens_clicks_and_waits_for_the_landing_animation():
    # Failure: returning while the landing animation still plays (the next click is dropped).
    game, presses = _teleporting_game()
    net = LodestoneNetwork(game, _Ctx())
    assert net.teleport(Lodestone.BURTHORPE, timeout_ms=2_000) is TeleportResult.ARRIVED
    assert game.snap.self_player.animation_id == -1
    assert game.queued == [(57, 1, -1, (1465 << 16) | 34), (57, 1, -1, (1092 << 16) | 12)]


def test_opening_is_retried_once_after_a_refusal():
    # The game refuses the network right after an arrival; the second press opens it.
    game, presses = _teleporting_game(open_on_attempt=2)
    import botwithus.api.lodestones as mod
    old = mod.OPEN_TIMEOUT_MS
    mod.OPEN_TIMEOUT_MS = 200
    try:
        result = LodestoneNetwork(game, _Ctx()).teleport(Lodestone.BURTHORPE, timeout_ms=2_000)
    finally:
        mod.OPEN_TIMEOUT_MS = old
    assert result is TeleportResult.ARRIVED and presses[0] == 2


def test_locked_and_not_in_game_send_nothing():
    game = FakeGame(FakeSnapshot(self_player=me()))
    assert LodestoneNetwork(game).teleport(Lodestone.LUNAR_ISLE) is TeleportResult.LOCKED
    game.snap.in_game = False
    assert LodestoneNetwork(game).teleport(Lodestone.BURTHORPE) is TeleportResult.NOT_IN_GAME
    assert game.queued == []


def test_teleport_to_previous_picks_the_op_named_after_the_lodestone():
    # Failure: looking for "Previous Destination" (the live op is "Teleport Burthorpe").
    game = FakeGame(FakeSnapshot(self_player=me(3222, 3218)))
    game.component_ops_map[HOME] = ops("Lodestone Network", "", "Teleport Burthorpe")

    def land(g):
        g.snap.self_player = me(2899, 3545)
    game.on_queue = land
    assert LodestoneNetwork(game, _Ctx()).teleport_to_previous(1_000) is TeleportResult.ARRIVED
    assert game.queued == [(57, 3, -1, (1465 << 16) | 34)]
    game2 = FakeGame(FakeSnapshot(self_player=me()))
    game2.component_ops_map[HOME] = ops("Lodestone Network")
    assert LodestoneNetwork(game2).teleport_to_previous(10) is TeleportResult.NETWORK_UNAVAILABLE


# --- derived events ---------------------------------------------------------------------

LOG, COINS = 1511, 995


def _frame(tick, items, xp=1000, level=10, banked=None, in_game=True):
    rows = [inv(93, 28, items)]
    if banked is not None:
        rows.append(inv(95, 10, banked))
    return FakeSnapshot(server_tick=tick, in_game=in_game, inventory_rows=rows,
                        self_player=me(skills=[skill(8, level, xp)]))


def test_inventory_change_events_carry_old_and_new_in_order():
    # Failure: old and new swapped, or an unchanged slot reported.
    out = []
    det = SnapshotChangeDetector(out.append)
    det.observe(_frame(10, {2: (LOG, 1), 5: (COINS, 100)}))
    det.observe(_frame(11, {5: (COINS, 130)}))
    assert out == [InventoryChanged(-1, 93, 2, InventoryItem(2, LOG, 1), InventoryItem(2, -1, 0), 11),
                   InventoryChanged(-1, 93, 5, InventoryItem(5, COINS, 100),
                                    InventoryItem(5, COINS, 130), 11)]
    assert out[0].is_removed and out[0].quantity_delta == -1
    assert out[1].quantity_delta == 30 and not out[1].is_added


def test_same_tick_frames_are_ignored_and_a_new_inventory_is_a_silent_baseline():
    # Failure: opening the bank flooding the bus with one event per banked slot.
    out = []
    det = SnapshotChangeDetector(out.append)
    det.observe(_frame(10, {}))
    det.observe(_frame(10, {0: (LOG, 1)}))       # same tick: not looked at
    det.observe(_frame(11, {}, banked={0: (COINS, 5), 1: (LOG, 9)}))
    assert out == []
    det.observe(_frame(12, {}, banked={0: (COINS, 6), 1: (LOG, 9)}))
    assert [(e.inv_id, e.slot) for e in out] == [(95, 0)]


def test_skill_changes_and_leaving_the_game_resets_the_baseline():
    out = []
    det = SnapshotChangeDetector(out.append)
    det.observe(_frame(10, {}, xp=1100, level=10))
    det.observe(_frame(11, {}, xp=1300, level=11))
    assert out == [SkillChanged(-1, 8, 1100, 1300, 10, 11, 10, 11, 11)]
    assert out[0].is_level_up and out[0].experience_gained == 200
    det.observe(_frame(12, {}, in_game=False))
    det.observe(_frame(40, {0: (LOG, 1)}, xp=9999, level=30))   # back in game: baseline only
    assert len(out) == 1


class _Ring:
    def __init__(self, batches):
        self.batches = list(batches)

    def poll(self):
        return self.batches.pop(0) if self.batches else []


def test_the_connection_pump_runs_the_detector_once_per_tick_event():
    # Failure: no derived events (detector never run), or a frame decoded on every poll.
    from botwithus.core.connection import Connection
    conn = Connection.__new__(Connection)
    frames = [_frame(10, {0: (LOG, 1)}), _frame(11, {})]
    reads = []
    seen = []
    conn.pid = 1
    conn._alive = True
    conn.events = EventBus()
    conn.events.subscribe(InventoryChanged, seen.append)
    conn._changes = SnapshotChangeDetector(conn.events.publish)
    conn._current_snapshot = lambda: reads.append(1) or frames[len(reads) - 1]
    conn._ring = _Ring([[ServerTick(0, 10)], [], [], [ServerTick(1, 11)], []])
    conn._pump_stop = threading.Event()
    pump = threading.Thread(target=conn._pump_loop, args=(0.005,))
    pump.start()
    threading.Event().wait(0.2)
    conn._pump_stop.set()
    pump.join(2)
    assert len(reads) == 2
    assert [(e.slot, e.new_item.item_id) for e in seen] == [(0, -1)]
