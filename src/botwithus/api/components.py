"""Interface component reads and clicks over the agent's component RPCs.

``GameApi.component`` returns the agent's component map as a dict. This module adds what a
script needs to *act* on a component: its live right-click ops, its child lists, and
:meth:`InterfaceComponent.interact`, which finds an op by the name the menu shows and queues
the COMPONENT action for it.

Ops come from ``get_component_ops`` (NXTLibrary PUBLIC_PROTOCOL.md section 6.7.2). They are the
menu as it stands now, read where the client builds its right-click menu, so they are the right
source for "which op is Drop". A cache option index is not: on the bronze arrow, Drop is cache
index 5 but op 8 on the wire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .actions import component_click
from ..core.rpc import MalformedReply

if TYPE_CHECKING:
    from .client_reads import Child
    from .game import GameApi

# The agent's "no sub-component" value for get_component_ops and the COMPONENT action.
NO_SUB = -1
# Interface and component ids are u16 on the agent; it refuses anything else.
_U16_MAX = 0xFFFF
# The ops the agent lists: 1..10. Bits 1..10 of ``mask`` are ops; the rest are other flags.
FIRST_OP = 1
LAST_OP = 10


def pack(iface: int, comp: int) -> int:
    """``(iface << 16) | comp``: the packed id the agent's ``parent_global`` and actions use."""
    return (iface << 16) | comp


def unpack(packed: int) -> tuple[int, int]:
    """The ``(iface, comp)`` of a packed id."""
    return packed >> 16, packed & _U16_MAX


def _require_u16(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _U16_MAX:
        raise ValueError(f"{name} must be an int in 0..{_U16_MAX}, got {value!r}")
    return value


def _require_sub(sub: int) -> int:
    if isinstance(sub, bool) or not isinstance(sub, int) or not NO_SUB <= sub <= _U16_MAX:
        raise ValueError(f"sub must be -1 (none) or 0..{_U16_MAX}, got {sub!r}")
    return sub


def _normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


@dataclass(frozen=True, slots=True)
class ComponentOp:
    """One right-click op. ``op`` is what the COMPONENT action takes as ``param1``.

    ``transmits`` is False for an op that runs only client-side script: clicking it does
    something on screen, but nothing reaches the server."""
    op: int
    name: str
    transmits: bool


@dataclass(frozen=True, slots=True)
class ComponentOps:
    """The ops a component lists right now, in ascending op order (``get_component_ops``).

    ``mask`` is the component's raw op-flags word: bit N set means op N reaches the server.
    It carries other flags too (bit 24: a list selection transmits), so read only bits
    1..10 as ops. An empty backpack slot has no ops: ``ops`` is empty, not an error."""
    mask: int
    ops: tuple[ComponentOp, ...]

    def op_named(self, name: str) -> int | None:
        """The op whose name is ``name``, compared case-insensitively with whitespace collapsed,
        or None. Exact match, not substring: "Drop" never matches "Drop all"."""
        wanted = _normalize(name)
        for entry in self.ops:
            if _normalize(entry.name) == wanted:
                return entry.op
        return None

    def names(self) -> list[str]:
        return [entry.name for entry in self.ops]

    @classmethod
    def from_reply(cls, reply: Any) -> "ComponentOps":
        if not isinstance(reply, dict):
            raise MalformedReply("get_component_ops", f"expected a map, got {reply!r}")
        try:
            ops = tuple(ComponentOp(int(o["op"]), str(o["name"]), bool(o["transmits"]))
                        for o in reply["ops"])
            return cls(int(reply["mask"]), ops)
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedReply("get_component_ops", f"malformed reply: {exc!r}") from exc


class InterfaceComponent:
    """A handle on one component, ``iface:comp`` and optionally one ``sub`` slot of it.

    Nothing is read when it is made: every method is one round trip, so read when you need it,
    not every tick. Get one from :meth:`GameApi.interface_component`.
    """

    __slots__ = ("_game", "iface", "comp", "sub")

    def __init__(self, game: "GameApi", iface: int, comp: int, sub: int = NO_SUB) -> None:
        self._game = game
        self.iface = _require_u16(iface, "iface")
        self.comp = _require_u16(comp, "comp")
        self.sub = _require_sub(sub)

    def __repr__(self) -> str:
        sub = "" if self.sub == NO_SUB else f"[{self.sub}]"
        return f"InterfaceComponent({self.iface}:{self.comp}{sub})"

    @property
    def packed(self) -> int:
        return pack(self.iface, self.comp)

    def read(self) -> dict[str, Any] | None:
        """The component map (``GameApi.component``), or None when it does not exist.

        ``get_component`` addresses iface and comp only: for a sub slot this is the map of the
        container, not of the slot. Use :meth:`ops` for a slot."""
        found = self._game.component(self.iface, self.comp)
        return None if found.get("iface", -1) == -1 else found

    def ops(self) -> ComponentOps:
        """The live right-click ops (``get_component_ops``, which does take ``sub``)."""
        return self._game.component_ops(self.iface, self.comp, self.sub)

    def static_children(self) -> list[int]:
        """Component ids of the children baked into the cache layout."""
        return self._game.static_children(self.iface, self.comp)

    def dynamic_children(self) -> list[int]:
        """One entry per child created at run time. See :meth:`GameApi.dynamic_children`:
        the entries are not usable ids. Use :meth:`dynamic_children_with_sub` instead."""
        return self._game.dynamic_children(self.iface, self.comp)

    def dynamic_children_with_sub(self) -> "tuple[Child, ...]":
        """The children created at run time as ``(comp, sub)``: ``sub`` addresses each one
        (:meth:`GameApi.dynamic_children_with_sub`)."""
        return self._game.dynamic_children_with_sub(self.iface, self.comp)

    def interact_op(self, op: int) -> None:
        """Queue the COMPONENT action with op ``op`` (1..10) on this component or slot."""
        if isinstance(op, bool) or not isinstance(op, int) or not FIRST_OP <= op <= LAST_OP:
            raise ValueError(f"op must be {FIRST_OP}..{LAST_OP}, got {op!r}")
        self._game.queue_action(component_click(self.iface, self.comp, option=op,
                                                sub_slot=self.sub))

    def interact(self, option_name: str) -> bool:
        """Click the op the menu shows as ``option_name`` (case-insensitive, exact).

        Two round trips: one to read the ops, one to queue the action. Returns False, with
        nothing queued, when no listed op has that name. A component that is not open raises
        :class:`~botwithus.core.rpc.RpcError` (``get_component_ops: not_found``); use
        :meth:`GameApi.interact_component` for the form that answers False then.

        The item-selection "Use" target verb and the "Wiki" entry are not ops 1..10, so they
        are never found here."""
        op = self.ops().op_named(option_name)
        if op is None:
            return False
        self.interact_op(op)
        return True
