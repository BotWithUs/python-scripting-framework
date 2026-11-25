"""Typed results of the agent reads that are RPC-only: obj vars, world spot anims and the
action-queue history. Each ``from_reply`` raises :class:`MalformedReply` for a reply that is not
the shape the agent's handler writes (NXTLibrary ``src/rpc/Handlers.cpp``)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .snapshot_types import Tile
from ..core.rpc import MalformedReply


def _rows(method: str, reply: Any, key: str | None) -> list[Any]:
    """The list under ``key`` (or the reply itself when ``key`` is None), or a raise."""
    rows = reply.get(key) if key is not None and isinstance(reply, dict) else reply
    if key is not None and not isinstance(reply, dict):
        rows = None
    if not isinstance(rows, list):
        raise MalformedReply(method, f"expected a list{'' if key is None else ' at ' + key!r}, "
                                     f"got {reply!r}")
    return rows


def _decode(method: str, fn, rows: list[Any]) -> tuple:
    try:
        return tuple(fn(row) for row in rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedReply(method, f"malformed reply: {exc!r}") from exc


@dataclass(frozen=True, slots=True)
class SlotObjVars:
    """The obj vars the item in one slot carries: ``vars`` maps var id to value (int32)."""
    slot: int
    vars: Mapping[int, int]

    def get(self, var_id: int, default: int | None = None) -> int | None:
        return self.vars.get(var_id, default)


def obj_vars_from_reply(reply: Any) -> tuple[SlotObjVars, ...]:
    """``get_obj_vars``: ``{slots: [{slot, vars: [{id, value}]}]}``, in the agent's order."""
    def one(row: dict) -> SlotObjVars:
        values = {int(v["id"]): int(v["value"]) for v in row["vars"]}
        return SlotObjVars(int(row["slot"]), MappingProxyType(values))
    return _decode("get_obj_vars", one, _rows("get_obj_vars", reply, "slots"))


@dataclass(frozen=True, slots=True)
class WorldSpotAnim:
    """A spot anim (graphic) playing on a tile, not on an entity (``query_spot_anims``).

    The agent reports no plane for these, so :attr:`tile` carries plane -1. Spot anims on an
    NPC or player are not listed here: they ride the snapshot (``spot_anim_id``) and the
    :class:`~botwithus.api.events.SpotAnim` event."""
    id: int
    tile_x: int
    tile_y: int

    @property
    def tile(self) -> Tile:
        return Tile(self.tile_x, self.tile_y, -1)


def spot_anims_from_reply(reply: Any) -> tuple[WorldSpotAnim, ...]:
    """``query_spot_anims``: ``{spot_anims: [{id, tile_x, tile_y}]}``, at most 256."""
    def one(row: dict) -> WorldSpotAnim:
        return WorldSpotAnim(int(row["id"]), int(row["tile_x"]), int(row["tile_y"]))
    return _decode("query_spot_anims", one, _rows("query_spot_anims", reply, "spot_anims"))


@dataclass(frozen=True, slots=True)
class ActionHistoryEntry:
    """One action the agent dispatched (``get_action_history``).

    ``timestamp`` is when it was dispatched, in the game machine's milliseconds since boot
    (``GetTickCount64``), not a wall-clock time: compare it only with another ``timestamp`` or
    with :meth:`GameApi.last_action_time`. ``delta`` is the milliseconds since the action before
    it, 0 for the first. Walk legs the native walker queued appear here too."""
    action_id: int
    param1: int
    param2: int
    param3: int
    timestamp: int
    delta: int


def action_history_from_reply(reply: Any) -> tuple[ActionHistoryEntry, ...]:
    """``get_action_history``: a top-level array of six-key maps."""
    def one(row: dict) -> ActionHistoryEntry:
        return ActionHistoryEntry(int(row["action_id"]), int(row["param1"]), int(row["param2"]),
                                  int(row["param3"]), int(row["timestamp"]), int(row["delta"]))
    return _decode("get_action_history", one, _rows("get_action_history", reply, None))
