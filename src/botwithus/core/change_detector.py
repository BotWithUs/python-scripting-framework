"""Derived per-tick events: inventory slot changes and skill changes.

Mirrors the Java host's ``core/shm/SnapshotChangeDetector`` (JBotWithUsV2, phase J2). The pump
calls :meth:`SnapshotChangeDetector.observe` with a fresh frame once per server tick (on the
agent's tick event, so a frame is decoded once per 600 ms, not every poll). It diffs the frame
against the previous tick's:

* an inventory not present on the previous tick is a silent baseline, so opening the bank does
  not flood the bus with one event per banked slot;
* leaving the game (lobby, login screen) drops the baseline, so logging back in does not report
  every slot and skill as changed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..api.events import GameEvent, InventoryChanged, SkillChanged
from ..api.snapshot_types import InventoryItem, SkillEntry

_EMPTY_ITEM_ID = -1
_NO_SEQ = -1


@dataclass(frozen=True, slots=True)
class TickState:
    server_tick: int
    inventories: dict[int, tuple[InventoryItem, ...]]
    skills: dict[int, SkillEntry]

    @classmethod
    def capture(cls, view: Any) -> "TickState | None":
        """The frame's inventories and skills; None out of game or with no player."""
        if not view.in_game:
            return None
        me = view.self_player
        if not me.valid:
            return None
        return cls(view.server_tick, {inv.inv_id: inv.items for inv in view.inventories()},
                   {s.type_id: s for s in me.skills})


class SnapshotChangeDetector:
    def __init__(self, sink: Callable[[GameEvent], None]) -> None:
        self._sink = sink
        self._previous: TickState | None = None

    def observe(self, view: Any) -> None:
        if self._previous is not None and view.server_tick == self._previous.server_tick:
            return
        self.observe_state(TickState.capture(view))

    def observe_state(self, current: TickState | None) -> None:
        if current is None:
            self._previous = None
            return
        before = self._previous
        if before is not None and current.server_tick != before.server_tick:
            self._inventory_changes(before, current)
            self._skill_changes(before, current)
        self._previous = current

    def _inventory_changes(self, before: TickState, after: TickState) -> None:
        for inv_id, now in after.inventories.items():
            old = before.inventories.get(inv_id)
            if old is None or old == now:
                continue
            for slot in range(max(len(old), len(now))):
                was, is_ = _slot(old, slot), _slot(now, slot)
                if was != is_:
                    self._sink(InventoryChanged(_NO_SEQ, inv_id, slot, was, is_,
                                                after.server_tick))

    def _skill_changes(self, before: TickState, after: TickState) -> None:
        for skill_id, now in after.skills.items():
            was = before.skills.get(skill_id)
            if was is None or was == now:
                continue
            self._sink(SkillChanged(_NO_SEQ, skill_id, was.experience, now.experience,
                                    was.actual_level, now.actual_level, was.boosted_level,
                                    now.boosted_level, after.server_tick))


def _slot(items: tuple[InventoryItem, ...], slot: int) -> InventoryItem:
    return items[slot] if slot < len(items) else InventoryItem(slot, _EMPTY_ITEM_ID, 0)
