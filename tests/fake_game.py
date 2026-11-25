"""A scripted stand-in for ``GameApi`` and its snapshot, for the J1-J3 composition API tests.

It answers the reads the composition APIs make (varps, varbits, varcs, component ops, interface
trees, components, scripts) from plain dicts, and records every queued action as
``(action_id, param1, param2, param3)``. Values are deliberately non-default, so a wrong id,
a swapped field or a wrong op gives a different answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

from botwithus.api.components import ComponentOp, ComponentOps
from botwithus.api.inventory import Backpack
from botwithus.api.snapshot_types import (GroundItem, Inventory, InventoryItem, LocalPlayer,
                                          LocationEntry, NpcEntry, PlayerEntry, SkillEntry)
from botwithus.api.variables import VarbitRead, VarpState
from botwithus.core.rpc import RpcError


def me(x=3200, y=3200, plane=0, *, skills=(), animation=-1, target=(-1, -1), member=True,
       server_index=77) -> LocalPlayer:
    return LocalPlayer(server_index=server_index, combat_level=88, tile_x=x, tile_y=y,
                       plane=plane, moving=False, following_index=-1, animation_id=animation,
                       stance_id=-1, target_index=target[0], target_type=target[1],
                       is_member=member, spot_anim_id=-1, skills=tuple(skills), valid=True)


def skill(type_id, level, xp, boosted=None) -> SkillEntry:
    return SkillEntry(type_id, xp, level, level if boosted is None else boosted)


def npc(index, type_id, x, y, *, plane=0, hp=0, max_hp=0, anim=-1, stance=-1, spot=-1,
        moving=False, combat=0, active=None) -> NpcEntry:
    return NpcEntry(index, type_id, x, y, plane, moving, -1, anim, stance, hp, max_hp, spot,
                    active_type_id=type_id if active is None else active, combat_level=combat)


def loc(type_id, x, y, *, plane=0, anim=-1, resolved=None, flags=0) -> LocationEntry:
    return LocationEntry(type_id, type_id, anim, x, y, plane, 10, 0, flags,
                         type_id if resolved is None else resolved)


def player(index, name, x, y, plane=0) -> PlayerEntry:
    return PlayerEntry(index, x, y, plane, False, -1, -1, -1, 50, -1, name=name)


def inv(inv_id, size, filled) -> Inventory:
    return Inventory(inv_id, tuple(InventoryItem(s, *filled.get(s, (-1, 0)))
                                   for s in range(size)))


@dataclass
class FakeSnapshot:
    self_player: LocalPlayer = field(default_factory=me)
    server_tick: int = 1000
    game_cycle: int = 50_000
    in_game: bool = True
    own_index: int = 77
    npc_rows: list = field(default_factory=list)
    player_rows: list = field(default_factory=list)
    loc_rows: list = field(default_factory=list)
    ground_rows: list = field(default_factory=list)
    inventory_rows: list = field(default_factory=list)
    open_ifaces: set = field(default_factory=set)

    def npcs(self):
        return list(self.npc_rows)

    def players(self):
        return list(self.player_rows)

    def locations(self):
        return list(self.loc_rows)

    def ground_items(self):
        return list(self.ground_rows)

    def inventories(self):
        return list(self.inventory_rows)

    def inventory(self, inv_id):
        return next((i for i in self.inventory_rows if i.inv_id == inv_id), None)

    def is_interface_open(self, iface):
        return iface in self.open_ifaces


def ops(*names, mask=None) -> ComponentOps:
    """Ops 1..n named ``names`` ("" skips a number)."""
    listed = tuple(ComponentOp(i, n, True) for i, n in enumerate(names, start=1) if n)
    bits = sum(1 << o.op for o in listed) if mask is None else mask
    return ComponentOps(bits, listed)


class FakeGame:
    def __init__(self, snapshot: FakeSnapshot | None = None) -> None:
        self.snap = snapshot or FakeSnapshot()
        self.varp_values: dict[int, int] = {}
        self.varbit_values: dict[int, int] = {}
        self.varc_values: dict[int, int] = {}
        # (iface, comp, sub) -> ComponentOps; an absent key raises not_found
        self.component_ops_map: dict[tuple[int, int, int], ComponentOps] = {}
        self.trees: dict[tuple[int, int], list[dict]] = {}
        self.component_maps: dict[tuple[int, int], dict] = {}
        # script id -> callable(int_args) -> returns list, or an exception to raise
        self.scripts: dict[int, Any] = {}
        self.queued: list[tuple[int, int, int, int]] = []
        self.calls: list[tuple] = []
        self.destroyed: list[int] = []
        self.health: tuple[int, int] | None = None
        self.on_queue: Callable[["FakeGame"], None] | None = None

    # --- snapshot ---------------------------------------------------------------------
    def snapshot(self):
        return self.snap

    # --- vars -------------------------------------------------------------------------
    def varp(self, var_id):
        self.calls.append(("varp", var_id))
        return self.varp_values.get(var_id, -1)

    def varps(self, ids):
        self.calls.append(("varps", list(ids)))
        return [self.varp_values.get(i, -1) for i in ids]

    def varbit(self, var_id):
        self.calls.append(("varbit", var_id))
        return self.varbit_values.get(var_id, -1)

    def read_varbits(self, ids):
        self.calls.append(("read_varbits", list(ids)))
        return [self.read_varbit(i) for i in ids]

    def read_varbit(self, var_id):
        if var_id in self.varbit_values:
            return VarbitRead(var_id, VarpState.SET, self.varbit_values[var_id], True)
        return VarbitRead(var_id, VarpState.UNAVAILABLE, -1, False)

    def read_varp(self, var_id):
        from botwithus.api.variables import VarKind, VarpRead
        if var_id in self.varp_values:
            v = self.varp_values[var_id]
            return VarpRead(var_id, VarpState.SET, v, v, VarKind.INT, True)
        return VarpRead(var_id, VarpState.UNAVAILABLE, -1, -1, VarKind.UNKNOWN, False)

    def varc_int(self, var_id):
        return self.varc_values.get(var_id, -1)

    def local_health(self):
        return self.health

    # --- components -------------------------------------------------------------------
    def component_ops(self, iface, comp, sub=-1):
        self.calls.append(("component_ops", iface, comp, sub))
        found = self.component_ops_map.get((iface, comp, sub))
        if found is None:
            raise RpcError("get_component_ops", "get_component_ops: not_found")
        return found

    def interface_tree(self, iface, comp):
        self.calls.append(("interface_tree", iface, comp))
        return {"count": len(self.trees.get((iface, comp), [])),
                "nodes": self.trees.get((iface, comp), [])}

    def component(self, iface, comp):
        return self.component_maps.get((iface, comp), {"iface": -1, "comp": -1})

    # --- scripts ----------------------------------------------------------------------
    def script_handle(self, script_id):
        self.calls.append(("script_handle", script_id))
        return 9000 + script_id if script_id in self.scripts else 0

    def execute_script(self, handle, int_args=None, returns=None, string_args=None):
        script = self.scripts[handle - 9000]
        self.calls.append(("execute_script", handle - 9000, list(int_args or []),
                           list(returns or [])))
        if isinstance(script, Exception):
            raise script
        return script(list(int_args or []))

    def destroy_script_handle(self, handle):
        self.destroyed.append(handle - 9000)

    # --- actions ----------------------------------------------------------------------
    def queue_action(self, action):
        self.queued.append((action.action_id, action.param1, action.param2, action.param3))
        if self.on_queue is not None:
            self.on_queue(self)

    def backpack(self):
        return Backpack(self)

    def logout(self):
        self.calls.append(("logout",))
        return True

    def exit_to_lobby(self):
        self.calls.append(("exit_to_lobby",))
        return True


def node(iface, comp, *, sub=-1, category=0, text="", sprite=-1, item=-1, amount=0,
         visible=True, parent=-1, triggers=()) -> dict:
    """One get_interface_tree node in the agent's keys."""
    return {"iface": iface, "comp": comp, "sub": sub, "category": category, "text": text,
            "sprite_id": sprite, "item_id": item, "item_amount": amount,
            "visible_global": visible, "parent": parent,
            "triggers": [{"type": t, "script_id": 1} for t in triggers]}


def moved(snap: FakeSnapshot, **changes) -> FakeSnapshot:
    return replace(snap, **changes)
