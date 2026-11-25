"""v25 live smoke: the server tile, hint arrows, overhead text and headbars, through this framework,
against a real injected agent. Developer tooling: pytest does not collect it, and it walks and
attacks with the character.

    set PYTHONPATH=<this repo>\\src
    bwu_host --py tests\\live\\v25_entity_smoke.py

Starts from the Lumbridge bank chest (3215,3256), like NXTLibrary's v25 scenarios, walks to the
Lumbridge fight, then to the Rimmington chicks, then back.

Each section prints PASS, FAIL or INCONCLUSIVE. INCONCLUSIVE means the game did not produce the
behaviour in the time allowed; it is not a pass. The exit code is non-zero if any section is not
a PASS. BWU_SMOKE_ONLY=overhead runs that section alone.
"""
import os
import sys
import time

from botwithus.api.actions import npc_action, walk_to
from botwithus.api.entities import HintArrows
from botwithus.api.game import GameApi
from botwithus.api.snapshot_types import EntityReadStatus
from botwithus.core import native
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView

BANK = (3215, 3256)
LEAD_START = (3222, 3235)
FIGHT = (3235, 3235)
CHICK_LEGS = [(3170, 3250), (3130, 3265), (3090, 3274), (3033, 3281)]

FIGHTER_TYPES = {32429, 12353, 11238, 11240}
DUCK_TYPES = {46, 2693}
CHICK_TYPE = 30870
ATTACK_SLOT = 2
HEALTH_BAR, ADRENALINE_BAR = 0, 7
FREE_PLAYER_INDEX = 2047

WALK_TIMEOUT_S = 90
FIGHTER_TIMEOUT_S = 120
SELF_BAR_TIMEOUT_S = 60
DUCK_KICK_S = 8
CHICK_TIMEOUT_S = 240
POLL_S = 0.05
MIN_LEAD_TILES = 1
MAX_WALK_LINES = 40

verdicts = []


def say(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


def verdict(section, result, detail):
    verdicts.append(result)
    say(f"{result:<12} {section}: {detail}")


def tile_text(t):
    return f"{t.x},{t.y},{t.plane}"


def same_tile(a, b):
    return (a.x, a.y, a.plane) == (b.x, b.y, b.plane)


class Smoke:
    def __init__(self, game):
        self.game = game

    def me(self):
        return self.game.snapshot().self_player

    def walk(self, to, on_sample=None):
        """Walk one queued hop. Returns the largest server-tile lead seen and whether it arrived."""
        self.game.queue_action(walk_to(*to))
        stop, lead = time.monotonic() + WALK_TIMEOUT_S, 0
        while time.monotonic() < stop:
            s = self.me()
            if s.has_server_tile:
                lead = max(lead, abs(s.tile_x - s.server_tile_x), abs(s.tile_y - s.server_tile_y))
            if on_sample:
                on_sample(s)
            if (s.tile_x, s.tile_y) == to:
                return lead, True
            time.sleep(POLL_S)
        return lead, False

    def own_row(self, snap, s):
        return next((p for p in snap.players() if p.server_index == s.server_index), None)

    def section_server_tile(self):
        _, arrived = self.walk(LEAD_START)
        if not arrived:
            return verdict("server tile", "FAIL", "did not reach the start tile")
        time.sleep(2)
        idle = self.me()
        say(f"idle: tile {tile_text(idle.tile)} server_tile {tile_text(idle.server_tile)}")
        idle_equal = same_tile(idle.tile, idle.server_tile)
        printed = [0]

        def show(s):
            if printed[0] < MAX_WALK_LINES and not same_tile(s.tile, s.server_tile):
                printed[0] += 1
                say(f"walking: tile {tile_text(s.tile)} server_tile {tile_text(s.server_tile)}")

        lead, arrived = self.walk(FIGHT, show)
        time.sleep(2)
        snap = self.game.snapshot()
        done = snap.self_player
        row = self.own_row(snap, done)
        say(f"arrived: tile {tile_text(done.tile)} server_tile {tile_text(done.server_tile)}")
        on_it = same_tile(done.tile, done.server_tile)
        row_ok = row is not None and same_tile(row.server_tile, done.server_tile)
        ok = arrived and idle_equal and lead >= MIN_LEAD_TILES and on_it and row_ok
        verdict("server tile", "PASS" if ok else "FAIL",
                f"max lead {lead} tiles, idle equal {idle_equal}, arrived on it {on_it}, "
                f"players[] row agrees {row_ok}")

    def section_hint_arrows(self):
        reply = self.game.hint_arrows()
        if not reply.ok:
            return verdict("hint arrows", "FAIL", f"refused: {reply.status.value}")
        for i, a in enumerate(reply.arrows):
            say(f"arrow {i}: source={a.source} slot={a.slot} type={a.type} "
                f"type_name={a.type_name} target_type={a.target_type} "
                f"target_index={a.target_index} plane={a.plane} tile={a.tile_x},{a.tile_y} "
                f"range={a.range_tiles} tile_property={a.tile}")
        here = self.me().tile
        placed = sum(1 for a in reply.arrows if a.tile is not None)
        within = HintArrows(self.game).within(here, 100_000).count()
        # An empty list proves the shape only; whether one exists depends on the character.
        result = "FAIL" if within != placed else ("PASS" if reply.arrows else "INCONCLUSIVE")
        verdict("hint arrows", result,
                f"{len(reply.arrows)} rows, truncated {reply.truncated}, tile arrows {placed}, "
                f"query within any distance {within}")

    def find_npc_bar(self, wanted, want_id, timeout):
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            for n in self.game.snapshot().npcs():
                if wanted(n):
                    bars = self.game.headbars("npc", n.server_index)
                    if any(b.headbar_id == want_id for b in bars.headbars):
                        return n, bars
            time.sleep(POLL_S)
        return None, None

    def kick_duck(self):
        for n in self.game.snapshot().npcs():
            if n.type_id in DUCK_TYPES:
                self.game.queue_action(npc_action(n, ATTACK_SLOT))
                return

    def find_self_bar(self, want_id, timeout):
        stop, next_kick = time.monotonic() + timeout, 0.0
        index = self.me().server_index
        while time.monotonic() < stop:
            if time.monotonic() >= next_kick:
                self.kick_duck()
                next_kick = time.monotonic() + DUCK_KICK_S
            bars = self.game.headbars("player", index)
            if any(b.headbar_id == want_id for b in bars.headbars):
                return bars
            time.sleep(POLL_S)
        return None

    def print_bars(self, label, bars):
        for b in bars.headbars:
            say(f"{label}: headbar_id={b.headbar_id} value={b.value} value_start={b.value_start} "
                f"start_cycle={b.start_cycle} duration={b.duration}")

    def section_headbars(self):
        missing = self.game.headbars("player", FREE_PLAYER_INDEX)
        say(f"headbars on a free player index: {missing.status.value}")
        if missing.status is not EntityReadStatus.NOT_FOUND:
            verdict("headbars code", "FAIL", missing.status.value)
        npc, bars = self.find_npc_bar(lambda n: n.type_id in FIGHTER_TYPES, HEALTH_BAR,
                                      FIGHTER_TIMEOUT_S)
        if npc is None:
            verdict("npc headbar", "INCONCLUSIVE", "no fighter showed a health bar in the window")
        else:
            self.print_bars(f"npc {npc.server_index}", bars)
            ok = all(0 <= b.value <= 255 for b in bars.headbars)
            verdict("npc headbar", "PASS" if ok else "FAIL",
                    f"type {npc.type_id}, {len(bars.headbars)} bars, truncated {bars.truncated}")
        own = self.find_self_bar(ADRENALINE_BAR, SELF_BAR_TIMEOUT_S)
        if own is None:
            verdict("self headbar", "INCONCLUSIVE", "no adrenaline bar on self in the window")
        else:
            self.print_bars("self", own)
            verdict("self headbar", "PASS", f"{len(own.headbars)} bars, id 7 present")

    def section_overhead_text(self):
        bad = self.game.overhead_text("npc", -1)
        say(f"overhead on index -1: status {bad.status.value}, text {bad.text!r}")
        # The first leg is only in reach from the bank: one queued walk stops at the scene edge.
        self.walk(BANK)
        for leg in CHICK_LEGS:
            _, arrived = self.walk(leg)
            if not arrived:
                return verdict("overhead text", "INCONCLUSIVE", "did not reach the chicks")
        stop, reads, silent = time.monotonic() + CHICK_TIMEOUT_S, 0, 0
        while time.monotonic() < stop:
            for n in self.game.snapshot().npcs():
                if n.type_id != CHICK_TYPE:
                    continue
                t = self.game.overhead_text("npc", n.server_index)
                reads += 1
                silent += 1 if t.ok and t.text is None else 0
                if t.text is not None:
                    say(f"chick {n.server_index} says {t.text!r}, expires in {t.expires_in_ms} ms, "
                        f"truncated {t.truncated}")
                    return verdict("overhead text", "PASS" if t.text == "Cheep!" else "FAIL",
                                   f"after {reads} reads, {silent} of them silent and not errors")
            time.sleep(POLL_S)
        verdict("overhead text", "INCONCLUSIVE", f"{reads} reads, {silent} silent, no text")

    def walk_back(self):
        for leg in reversed(CHICK_LEGS[:-1]):
            self.walk(leg)
        _, arrived = self.walk(BANK)
        say(f"back at the bank: {arrived}")


def main():
    pids = native.discover_pids()
    if not pids:
        raise SystemExit("no client with an agent")
    host = NativeHost.attach(pids[0])
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    smoke = Smoke(game)
    start = smoke.me()
    say(f"start tile {tile_text(start.tile)}, in game {game.snapshot().in_game}, "
        f"server tile {tile_text(start.server_tile)}")
    if (start.tile_x, start.tile_y) != BANK:
        raise SystemExit(f"stand on the Lumbridge bank chest tile {BANK} first")
    if os.environ.get("BWU_SMOKE_ONLY") != "overhead":
        smoke.section_server_tile()
        smoke.section_hint_arrows()
        smoke.section_headbars()
    smoke.section_overhead_text()
    smoke.walk_back()
    bad = sum(1 for v in verdicts if v != "PASS")
    say(f"{len(verdicts)} sections, {bad} not PASS")
    sys.exit(1 if bad else 0)


main()
