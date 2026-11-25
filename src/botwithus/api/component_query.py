"""Queries over an interface's component tree, and clicking a component by its option name.

Mirrors the Java host's ``component/ComponentQuery`` and ``ComponentNode`` (JBotWithUsV2, phase
J3). A query fetches the tree once (one ``get_interface_tree`` round trip, when a terminal first
needs it); ops and item definitions are read at most once per node and item within the query.

    node = game.components(1092).with_option("Teleport").first()
    if node is not None:
        node.interact("Teleport")

:meth:`ComponentNode.interact` sends the node's own ``sub`` in ``param2``, so it works on a slot
of a grid or a dynamic child as well as on a plain component (whose sub is -1).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable

from .actions import component_click
from .components import ComponentOps

if TYPE_CHECKING:
    from .game import GameApi

NO_ITEM = -1
DEFAULT_OP = 1
ROOT_COMPONENT = 0
_OPS_NOT_FOUND = "get_component_ops: not_found"


class ComponentNode:
    """One component of a fetched tree (the agent's component map, as ``data``)."""

    __slots__ = ("_game", "data")

    def __init__(self, game: "GameApi", data: dict[str, Any]) -> None:
        self._game = game
        self.data = data

    def __repr__(self) -> str:
        sub = "" if self.sub == -1 else f"[{self.sub}]"
        return f"ComponentNode({self.iface}:{self.comp}{sub})"

    @property
    def iface(self) -> int:
        return int(self.data.get("iface", -1))

    @property
    def comp(self) -> int:
        return int(self.data.get("comp", -1))

    @property
    def sub(self) -> int:
        return int(self.data.get("sub", -1))

    @property
    def category(self) -> int:
        return int(self.data.get("category", -1))

    @property
    def text(self) -> str:
        return str(self.data.get("text") or "")

    @property
    def sprite_id(self) -> int:
        return int(self.data.get("sprite_id", -1))

    @property
    def item_id(self) -> int:
        return int(self.data.get("item_id", NO_ITEM))

    @property
    def item_amount(self) -> int:
        return int(self.data.get("item_amount", 0))

    @property
    def is_visible_global(self) -> bool:
        return bool(self.data.get("visible_global", False))

    def ops(self) -> ComponentOps:
        """The live ops of this node (its own sub). A component that is gone lists none."""
        from ..core.rpc import RpcError
        try:
            return self._game.component_ops(self.iface, self.comp, self.sub)
        except RpcError as exc:
            if exc.agent_message == _OPS_NOT_FOUND:
                return ComponentOps(0, ())
            raise

    def options(self) -> list[str]:
        return self.ops().names()

    def has_option(self, option: str) -> bool:
        return self.ops().op_named(option) is not None

    def interact(self, option: str) -> bool:
        """Queue the op named ``option`` (case-insensitive, exact) with this node's sub. False,
        with nothing sent, when the node lists no such op."""
        return self._interact(self.ops(), option)

    def _interact(self, ops: ComponentOps, option: str) -> bool:
        op = ops.op_named(option)
        if op is None:
            return False
        self._game.queue_action(component_click(self.iface, self.comp, option=op,
                                                sub_slot=self.sub))
        return True


class ComponentQuery:
    """A filtered view of the tree under ``(iface, comp)``."""

    def __init__(self, game: "GameApi", iface: int, comp: int = ROOT_COMPONENT,
                 item_defs: Callable[[int], Any] | None = None) -> None:
        self._game = game
        self._root = (iface, comp)
        self._filters: list[Callable[[ComponentNode], bool]] = []
        self._limit: int | None = None
        self._nodes: list[ComponentNode] | None = None
        self._ops: dict[tuple[int, int, int], ComponentOps] = {}
        self._item_defs = item_defs
        self._items: dict[int, Any] = {}

    # --- filters ----------------------------------------------------------------------

    def where(self, predicate: Callable[[ComponentNode], bool]) -> "ComponentQuery":
        self._filters.append(predicate)
        return self

    def with_id(self, comp: int) -> "ComponentQuery":
        return self.where(lambda n: n.comp == comp)

    def with_category(self, *categories: int) -> "ComponentQuery":
        wanted = frozenset(categories)
        return self.where(lambda n: n.category in wanted)

    def with_text(self, text: str) -> "ComponentQuery":
        wanted = text.casefold()
        return self.where(lambda n: n.text.casefold() == wanted)

    def containing_text(self, needle: str) -> "ComponentQuery":
        wanted = needle.casefold()
        return self.where(lambda n: wanted in n.text.casefold())

    def text_matching(self, pattern: "str | re.Pattern[str]") -> "ComponentQuery":
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        return self.where(lambda n: compiled.fullmatch(n.text) is not None)

    def with_sprite_id(self, sprite_id: int) -> "ComponentQuery":
        return self.where(lambda n: n.sprite_id == sprite_id)

    def with_item_id(self, item_id: int) -> "ComponentQuery":
        return self.where(lambda n: n.item_id == item_id)

    def with_option(self, *options: str) -> "ComponentQuery":
        """Nodes listing any of ``options`` (one ``get_component_ops`` per node, memoised)."""
        if not options:
            return self.where(lambda n: False)
        return self.where(lambda n: any(self._ops_of(n).op_named(o) is not None
                                        for o in options))

    def has_default_option(self) -> "ComponentQuery":
        """Nodes whose op 1 reaches the server (its mask bit, v1's flag)."""
        return self.where(lambda n: bool(self._ops_of(n).mask & (1 << DEFAULT_OP)))

    def with_item_name(self, name: "str | re.Pattern[str]") -> "ComponentQuery":
        """Nodes holding an item whose name contains ``name`` (ignoring case), or in which a
        Pattern is found. As the Java host: a substring, not the whole name."""
        if isinstance(name, re.Pattern):
            return self.where(lambda n: (d := self._item_of(n)) is not None
                              and name.search(d.name or "") is not None)
        wanted = name.casefold()
        return self.where(lambda n: (d := self._item_of(n)) is not None
                          and wanted in (d.name or "").casefold())

    def with_item_category(self, category: int) -> "ComponentQuery":
        return self.where(lambda n: (d := self._item_of(n)) is not None
                          and d.category == category)

    def with_item_amount(self, low: int, high: int) -> "ComponentQuery":
        return self.where(lambda n: n.item_id != NO_ITEM and low <= n.item_amount <= high)

    def visible(self) -> "ComponentQuery":
        return self.where(lambda n: n.is_visible_global)

    def hidden(self) -> "ComponentQuery":
        return self.where(lambda n: not n.is_visible_global)

    def limit(self, count: int) -> "ComponentQuery":
        self._limit = max(0, count)
        return self

    # --- terminals --------------------------------------------------------------------

    def all(self) -> list[ComponentNode]:
        out: list[ComponentNode] = []
        for node in self._materialize():
            if all(f(node) for f in self._filters):
                out.append(node)
                if self._limit is not None and len(out) >= self._limit:
                    break
        return out

    def first(self) -> ComponentNode | None:
        return next((n for n in self._materialize() if all(f(n) for f in self._filters)), None)

    def count(self) -> int:
        return len(self.all())

    def exists(self) -> bool:
        return self.first() is not None

    def interact_first(self, option: str) -> bool:
        """Click ``option`` on the first matching node that lists it."""
        node = self.with_option(option).first()
        return node is not None and node._interact(self._ops_of(node), option)

    # --- helpers ----------------------------------------------------------------------

    def _materialize(self) -> list[ComponentNode]:
        if self._nodes is None:
            tree = self._game.interface_tree(*self._root)
            raw = tree.get("nodes") if isinstance(tree, dict) else None
            self._nodes = [ComponentNode(self._game, d) for d in raw or () if isinstance(d, dict)]
        return self._nodes

    def _ops_of(self, node: ComponentNode) -> ComponentOps:
        key = (node.iface, node.comp, node.sub)
        if key not in self._ops:
            self._ops[key] = node.ops()
        return self._ops[key]

    def _item_of(self, node: ComponentNode) -> Any:
        if node.item_id == NO_ITEM:
            return None
        if node.item_id not in self._items:
            from .inventory import _item_defs
            self._items[node.item_id] = _item_defs(self._item_defs)(node.item_id)
        return self._items[node.item_id]
