"""A minimal plugin-style script: report state, count nearby NPCs, walk to a tile.

Drop this file (or a copy) into ``~/.botwithus/scripts/python/`` and it appears in the
``bwu_host`` GUI, with the settings below in its panel. The file defines exactly one
``BotScript`` and does nothing at import time, which is what the GUI needs: it imports every
script once to list it. It demonstrates the snapshot read, entity queries, config fields, tick
pacing and (when WorldWalker is available) navigation.

To run it from a console instead, see the README quickstart's ``__main__`` block.
"""
from __future__ import annotations

from botwithus.api import (
    BotScript, Npcs, ScriptContext, Tile, bool_field, int_field, script_manifest)


@script_manifest(name="Example", version="1.1", author="botwithus",
                 description="Reports state and walks to Lumbridge bank.", category="Other")
class Example(BotScript):
    GOAL = Tile(3208, 3220, 0)

    def get_config_fields(self):
        return [
            int_field("radius", "NPC count radius", 8, min=1, max=32,
                      description="How far around the player to count NPCs, in tiles"),
            bool_field("walk", "Walk to the bank", True),
        ]

    def on_config_update(self, config: dict) -> None:
        # Called once before on_start, then after every change made in the GUI.
        self.radius = config["radius"]
        self.walk = config["walk"]

    def on_start(self, ctx: ScriptContext) -> None:
        self.ctx = ctx
        self.arrived = False

    def on_loop(self) -> int:
        snap = self.ctx.snapshot()
        me = snap.self_player
        if not me.has_tile:
            return 1000  # wait for a player with a tile: neither state 30 nor me.valid means one

        near = Npcs(snap).within(me.tile, self.radius).count()
        self.ctx.log.info("tick=%s tile=%s npcs<=%s=%s", snap.server_tick, me.tile,
                          self.radius, near)

        nav = self.ctx.navigation
        if self.walk and nav is not None and not self.arrived:
            self.arrived = nav.walk_to(self.GOAL, radius=2)
            if self.arrived:
                self.ctx.log.info("arrived at goal")

        # Pace on server ticks -- never on game_cycle / publish_seq. False means a stop.
        if not self.ctx.sleep_ticks(1):
            return -1
        return 0

    def on_stop(self) -> None:
        self.ctx.log.info("stopped")
