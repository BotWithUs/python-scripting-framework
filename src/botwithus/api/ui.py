"""Plugin UI: native game components the client draws itself (``ctx.ui``).

An opt-in, additional UI route. It is the agent's ``ui.*`` verbs (NXTLibrary
``PUBLIC_PROTOCOL.md`` 6.12) through the native host's ``botwithus._native.ui_*``. Nothing here
touches the config schema, the debug-draw overlay or any other script UI.

    root = ctx.ui.component(1477, 61)                 # a Jagex parent: TOPLEVEL_V2__GAME_AREA
    panel = ctx.ui.layer(root, x=40, y=40, width=320, height=140)
    label = ctx.ui.text(root, "hello", x=48, y=48, width=300, height=20, rgba=rgba(255, 255, 0))
    canvas = ctx.ui.canvas(root, x=40, y=80, width=320, height=100)
    with canvas.draw() as d:                          # one ui.canvas call for the whole batch
        d.rect(0, 0, 320, 100, rgba=0x20202080)
        d.line(10, 10, 310, 90, thickness=3, rgba=0xFF0000FF)
    label.set(text="hello again")
    print(label.get(content=True).content)
    ctx.events.subscribe(UiEvent, on_ui)              # every ui event for these

Interactive components (plugin UI P2, NXTLibrary a17edc4 or later; :attr:`PluginUi.has_p2`):

    box = ctx.ui.panel(root, vertical=True, x=40, y=40, width=220, height=260)
    go = ctx.ui.button(box, "Start", width=200, height=30)
    go.on_pressed(lambda e: print("pressed", e.current))        # a UiButtonPressed
    trees = ctx.ui.list(box, [UiEntry(1, "Oak"), UiEntry(2, "Yew")], selection_limit=1)
    trees.on_selection_changed(lambda e: print(e.entry_id, e.selected))
    name = ctx.ui.input_field(box, width=200, height=30, max_length=20)
    name.on_content_changed(lambda e: print(e.reason, e.content))
    name.blur()                                        # give the game its keys back

A hook's events arrive typed (:class:`~botwithus.api.events.UiClick`,
:class:`~botwithus.api.events.UiButtonPressed`, :class:`~botwithus.api.events.UiContentChanged`,
:class:`~botwithus.api.events.UiSliderChanged`,
:class:`~botwithus.api.events.UiEntrySelectionChanged`, ...), all subclasses of
:class:`~botwithus.api.events.UiEvent`, through the same routing as ``destroyed``. **Setting a
value never raises the change hook** (a slider value, a combo or list selection): only the
user's input does. The one exception is an input_field ``content`` set, which raises
``oncontentchanged`` like typing.

**Errors reach the script as the native host raises them.** ``UiRefused`` (a ``RuntimeError``)
is the agent's refusal, its text verbatim (``"ui type not supported yet: button"``,
``"ui: unknown handle (destroyed, invalidated or not yours)"``, ...). ``ValueError`` is a
refusal the host made without sending, for what the agent refuses on every component anyway (a
forbidden prop such as ``op_config``, category 0, editing a Jagex component), with the agent's
own text. ``TimeoutError`` ("rpc_timeout"): no answer in time, so the outcome is unknown. A
lost client is :class:`~botwithus.core.native.ClientExited` / ``ConnectionError``.

**Lifetime.** A script's components are its own. When the script stops (after ``on_stop``) the
framework destroys exactly the components it made, with one ``ui.destroy`` each, so other
scripts on the same client keep theirs. The native host's own release on detach, and the agent's
on pipe close, stay the backstop. A Jagex handle (:meth:`PluginUi.component`) is a lookup, not a
component: nothing is destroyed for it.

**Subscriptions** (``ui.subscribe``) are the script's too. A destroyed component takes its
subscriptions with it, on both sides, so neither a destroy, a ``destroyed`` event nor the
release at stop sends ``ui.unsubscribe``; unsubscribing a component that is already gone is a
success that sends nothing.

**Unsubscribing drops events still queued.** Once the agent answers ``ui.unsubscribe``, the
native host discards that hook's events for the component that it had not handed over yet, so
a click made just before you unsubscribe may never reach a handler. Events the ui event thread
had already polled are not dropped: they skip a handler that ``off()`` removed, but still reach
:meth:`PluginUi.subscribe` handlers, ``ctx.events``, and any :meth:`Component.on` handler still
registered (``unsubscribe_hook`` alone removes none).

**Types.** :class:`ComponentType` lists what the agent creates. :meth:`PluginUi.create` takes
any type name, so a type the agent adds later works with no change here: until then the agent
refuses it with ``"ui type not supported yet: <type>"``, and a type with no wrapper class of
its own comes back as a plain :class:`Component`. On an agent before P2 the interactive types
are refused that way too.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Union

from .events import (UiButtonPressed, UiClick, UiContentChanged, UiEntrySelectionChanged,
                     UiEvent, UiHook, UiMouseLeave, UiMouseOver, UiRelease, UiSliderChanged)

if TYPE_CHECKING:  # pragma: no cover
    from ..core.native import NativeHost

_log = logging.getLogger("botwithus.ui")

# The agent's limits (PUBLIC_PROTOCOL 6.12).
CANVAS_OPS_MAX = 128          # ops per ui.canvas call; a longer batch is sent in chunks
KEY_STATIC = -1               # ui.get_component's key for a static component
ORDER_FRONT = "front"
ORDER_BACK = "back"
_BYTE_MAX = 0xFF
_RGBA_SHIFTS = (24, 16, 8, 0)


class ComponentType(str, Enum):
    """The component types the agent creates today. A ``str``, so it compares equal to and is
    sent as its name; :meth:`PluginUi.create` also takes a plain string for a later type."""
    CANVAS = "canvas"
    RECTANGLE = "rectangle"
    LINE = "line"
    TEXT = "text"
    LAYER = "layer"
    # P2: the interactive types (NXTLibrary a17edc4).
    BUTTON = "button"
    CHECK = "check"
    LIST = "list"
    INPUT_FIELD = "input_field"
    SLIDER = "slider"
    COMBO_FIELD = "combo_field"
    PANEL = "panel"
    GRID = "grid"


def rgba(r: int, g: int, b: int, a: int = _BYTE_MAX) -> int:
    """A colour as the agent takes it: ``0xRRGGBBAA``. Each channel is 0..255."""
    channels = (r, g, b, a)
    for value in channels:
        if not 0 <= value <= _BYTE_MAX:
            raise ValueError(f"rgba: channel {value} is outside 0..{_BYTE_MAX}")
    out = 0
    for value, shift in zip(channels, _RGBA_SHIFTS):
        out |= value << shift
    return out


@dataclass(frozen=True, slots=True)
class UiEntry:
    """One combo_field or list entry, as a script sets it: ``id`` unique within the component,
    ``text`` at most 255 bytes of UTF-8."""
    id: int
    text: str


@dataclass(frozen=True, slots=True)
class UiEntryRead:
    """One entry as ``ui.get`` reads it back. ``hidden``, ``disabled`` and ``selected`` are a
    list's; a combo_field's are False (its selection is :attr:`UiReadback.selected`)."""
    id: int
    text: str
    truncated: bool = False
    hidden: bool = False
    disabled: bool = False
    selected: bool = False

    @classmethod
    def from_native(cls, d: Mapping[str, Any]) -> "UiEntryRead":
        return cls(id=int(d["id"]), text=str(d["text"]), truncated=bool(d.get("truncated")),
                   hidden=bool(d.get("hidden")), disabled=bool(d.get("disabled")),
                   selected=bool(d.get("selected")))


EntryLike = Union[UiEntry, tuple, Mapping[str, Any]]


def _entry_wire(entry: EntryLike) -> dict[str, Any]:
    """An entry as the native host takes it: ``{"id": int, "text": str}``."""
    if isinstance(entry, UiEntry):
        return {"id": entry.id, "text": entry.text}
    if isinstance(entry, Mapping):
        return {"id": entry["id"], "text": entry["text"]}
    entry_id, text = entry
    return {"id": entry_id, "text": text}


def _wire_props(props: Mapping[str, Any]) -> dict[str, Any]:
    """``props`` as the native host takes them: ``entries`` as a list of ``{id, text}``, and a
    list's ``selected`` (any iterable of ids, not an int) as a list."""
    out = dict(props)
    if "entries" in out and out["entries"] is not None:
        out["entries"] = [_entry_wire(e) for e in out["entries"]]
    selected = out.get("selected")
    if selected is not None and not isinstance(selected, (int, str, list)):
        out["selected"] = list(selected)
    return out


# ui.get keys every component has (P1). The P2 keys appear only for the type that has them.
_READBACK_KEYS = ("handle", "parent", "type", "type_name", "iface", "comp", "key", "jagex",
                  "quarantined", "x", "y", "width", "height", "rgba", "visible", "restricted",
                  "clickthrough", "num_elements", "line_width", "line_invert", "fill",
                  "outline_thickness", "use_count", "op_flags_read", "op_mask", "op_target")
_WIDGET_KEYS = ("enabled", "label", "label_truncated", "can_toggle", "toggled", "checked",
                "focused", "filter", "max_length", "font_id", "sprite_id", "renders", "min",
                "max", "value", "step", "ticks", "selection_limit", "vertical")


@dataclass(frozen=True, slots=True)
class UiReadback:
    """``ui.get``: the handle map plus the component's state. Geometry is post-layout and
    parent relative. A field that does not apply to the type is -1 (``num_elements``,
    ``line_width``, ``line_invert``, ``fill``, ``outline_thickness``). ``content`` is a text
    component's or an input_field's text, only when asked for (``get(content=True)``), else
    None. ``raw`` is the dict the host returned, for a key added later.

    The interactive (P2) fields are None on a type that does not have them: ``enabled`` (every
    agent component); button ``label``, ``label_truncated``, ``can_toggle``, ``toggled``; check
    ``checked``; input_field ``focused``, ``filter``, ``max_length``, ``font_id``,
    ``sprite_id``, ``renders``; slider ``min``, ``max``, ``value``, ``step``, ``ticks``;
    combo_field and list ``entries``; combo_field ``selected`` (an entry id, -1 none); list
    ``selected`` (a tuple of ids) and ``selection_limit``; panel ``vertical``.

    ``renders`` is reported as the agent reads it: on 951-4 an agent input_field does not draw,
    not even its text, until the agent found a background sprite to copy, and in the world
    there usually is none (``renders`` False). Content, events and focus work regardless."""
    handle: int
    parent: int
    type: int
    type_name: str
    iface: int
    comp: int
    key: int
    jagex: bool
    quarantined: bool
    x: int
    y: int
    width: int
    height: int
    rgba: int
    visible: bool
    restricted: bool
    clickthrough: bool
    num_elements: int
    line_width: int
    line_invert: int
    fill: int
    outline_thickness: int
    use_count: int
    op_flags_read: bool
    op_mask: int
    op_target: int
    content: str | None
    content_truncated: bool
    raw: Mapping[str, Any]
    enabled: bool | None = None
    label: str | None = None
    label_truncated: bool | None = None
    can_toggle: bool | None = None
    toggled: bool | None = None
    checked: bool | None = None
    focused: bool | None = None
    filter: str | None = None
    max_length: int | None = None
    font_id: int | None = None
    sprite_id: int | None = None
    renders: bool | None = None
    min: int | None = None
    max: int | None = None
    value: int | None = None
    step: int | None = None
    ticks: int | None = None
    entries: tuple[UiEntryRead, ...] | None = None
    selected: int | tuple[int, ...] | None = None
    selection_limit: int | None = None
    vertical: bool | None = None

    @classmethod
    def from_native(cls, d: Mapping[str, Any]) -> "UiReadback":
        values = {name: d[name] for name in _READBACK_KEYS}
        widget = {name: d[name] for name in _WIDGET_KEYS if name in d}
        if d.get("entries") is not None:
            widget["entries"] = tuple(UiEntryRead.from_native(e) for e in d["entries"])
        selected = d.get("selected")
        if selected is not None:
            widget["selected"] = selected if isinstance(selected, int) else tuple(selected)
        return cls(**values, **widget, content=d.get("content"),
                   content_truncated=bool(d.get("content_truncated", False)),
                   raw=MappingProxyType(dict(d)))


class _Handle:
    """What every handle has: its ids, and ``get``."""

    __slots__ = ("_ui", "handle", "parent", "type", "type_name", "iface", "comp", "key")

    def __init__(self, ui: "PluginUi", info: Mapping[str, Any]) -> None:
        self._ui = ui
        self.handle: int = int(info["handle"])
        self.parent: int = int(info.get("parent", 0))
        self.type: int = int(info.get("type", -1))
        self.type_name: str = str(info.get("type_name", ""))
        self.iface: int = int(info.get("iface", -1))
        self.comp: int = int(info.get("comp", -1))
        self.key: int = int(info.get("key", KEY_STATIC))

    is_jagex = False

    def get(self, *, content: bool = False) -> UiReadback:
        """``ui.get``. ``content=True`` also reads a text component's text."""
        return UiReadback.from_native(self._ui._call("get", self.handle, bool(content)))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Handle) and other.handle == self.handle

    def __hash__(self) -> int:
        return hash(self.handle)

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(handle={self.handle}, type={self.type_name!r}, "
                f"iface={self.iface}, comp={self.comp}, key={self.key})")


class JagexComponent(_Handle):
    """A Jagex component, from :meth:`PluginUi.component`. Read-only: usable as a parent and
    with :meth:`get`, nothing else (the agent refuses every edit of a Jagex component)."""

    __slots__ = ()
    is_jagex = True

    @property
    def alive(self) -> bool:
        """False once a ``destroyed`` event for it arrived (its interface closed, say). The
        agent hands every script on one client the same handle for the same component, so the
        event reaches every script that looked it up."""
        return self._ui._holds_jagex(self.handle)


class Component(_Handle):
    """A component this script made. The base of the typed wrappers, and what a type with no
    wrapper of its own (a later type) comes back as."""

    __slots__ = ()

    def set(self, **props: Any) -> None:
        """``ui.set``: apply ``props`` (``x``, ``y``, ``width``, ``height``, ``rgba``,
        ``clickthrough``, ``enabled``, the ``*_pc`` numbers, and the type's own). All or nothing:
        an unknown or refused prop fails the call and applies nothing. ``entries`` takes
        :class:`UiEntry`, ``(id, text)`` or ``{"id", "text"}`` items."""
        self._ui._call("set", self.handle, _wire_props(props))

    def order(self, to: str) -> None:
        """``ui.order``: ``"front"`` or ``"back"`` among its siblings."""
        self._ui._call("order", self.handle, to)

    def to_front(self) -> None:
        self.order(ORDER_FRONT)

    def to_back(self) -> None:
        self.order(ORDER_BACK)

    def destroy(self) -> bool:
        """``ui.destroy``. Returns the agent's ``freed``. The handle is the script's no longer
        after any answer from the agent, a refusal included.

        The children of a destroyed component go with it in the game, but the agent sends no
        ``destroyed`` for an explicit destroy (PUBLIC_PROTOCOL 7.3), and this framework does not
        track parentage, so a child's :attr:`alive` stays True; a call on it is then refused
        with ``UiRefused`` "unknown handle", and the script's release skips it."""
        return self._ui._destroy(self.handle)

    def subscribe(self, handler: Callable[[UiEvent], None], *, hook: str | None = None
                  ) -> Callable[[], None]:
        """``handler`` for this component's :class:`UiEvent`\\ s (only ``hook``, if given).
        A local filter only: it sends nothing, so a P2 hook's events arrive only once the hook
        is subscribed on the agent (:meth:`on` does both, :meth:`subscribe_hook` the latter)."""
        return self._ui.subscribe(handler, handle=self.handle, hook=hook)

    def subscribe_hook(self, hook: str) -> None:
        """``ui.subscribe``: have the agent push ``hook`` (a :class:`~botwithus.api.events.UiHook`
        name) for this component. Subscribing twice is harmless. The agent refuses a hook it does
        not know or that does not fit the type, verbatim (``UiRefused``)."""
        self._ui._subscribe_hook(self.handle, hook)

    def unsubscribe_hook(self, hook: str) -> bool:
        """``ui.unsubscribe``: True if it was subscribed. No push for ``hook`` arrives after this
        returns: the host also discards the hook's events it had not handed over yet, so a click
        made just before may never be delivered. Events the ui event thread already polled are
        still delivered to every handler that wants them: this removes no :meth:`on` handler
        (``off()`` does). On a component already gone (destroyed, or the script stopped) it is a
        success that sends nothing: the agent dropped its subscriptions with it, so it returns
        False. A ``TimeoutError`` leaves the hook subscribed on the host."""
        return self._ui._unsubscribe_hook(self.handle, hook)

    def hooks(self) -> frozenset[str]:
        """The hooks this script subscribed on this component, as far as this framework knows."""
        return self._ui._hooks_of(self.handle)

    def on(self, hook: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """``handler`` for this component's ``hook`` events, subscribing the hook on the agent
        (``destroyed`` needs no subscribe). Returns ``off()``, which removes the handler and,
        once no :meth:`on` handler for the hook is left, unsubscribes it; ``off()`` never
        raises. The handler gets the hook's typed event, on the client's ui event thread. The
        typed ``on_*`` helpers return the same ``off()``.

        Once ``off()`` returns, its handler is never called again, even for an event already
        polled. When ``off()`` unsubscribes the hook, the host discards the hook's events it had
        not handed over yet, so a click made just before ``off()`` may never reach any handler.
        An event already polled still reaches :meth:`PluginUi.subscribe` handlers and
        ``ctx.events``. If the unsubscribe times out (logged, not raised), the handler is gone
        anyway, but the host keeps delivering the hook to those.

        ``hook`` must be a :data:`UiHook.SUBSCRIBABLE` name or ``destroyed``; anything else is a
        ``ValueError`` and nothing is sent (:meth:`subscribe_hook` passes any name through, for
        a hook the agent adds later).

        Once the script stops, the framework waits for a handler still running before it calls
        ``on_stop``. So a handler must not block on something ``on_stop`` (or the script's
        stop) would set: it would hang the stop. A handler should not stop or close its own
        script's connection synchronously either: it works (the stop does not wait for the
        script from inside its own handler, and ``close()`` leaves the detach to whichever
        thread ends last), but the stop only completes after the handler returns."""
        if hook != UiHook.DESTROYED and hook not in UiHook.SUBSCRIBABLE:
            raise ValueError(f"ui: {hook!r} is not a hook on() takes: "
                             f"{', '.join(sorted(UiHook.SUBSCRIBABLE))} or destroyed")
        return self._ui._on(self.handle, hook, handler, None)

    def on_click(self, handler: Callable[[UiClick], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONCLICK, handler, UiClick)

    def on_release(self, handler: Callable[[UiRelease], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONRELEASE, handler, UiRelease)

    def on_mouse_over(self, handler: Callable[[UiMouseOver], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONMOUSEOVER, handler, UiMouseOver)

    def on_mouse_leave(self, handler: Callable[[UiMouseLeave], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONMOUSELEAVE, handler, UiMouseLeave)

    def set_enabled(self, enabled: bool) -> None:
        self.set(enabled=bool(enabled))

    @property
    def alive(self) -> bool:
        """False once destroyed, or once a ``destroyed`` event for it arrived."""
        return self._ui._owns(self.handle)


class Layer(Component):
    """``layer``: a container; make other components under it."""
    __slots__ = ()


class Rectangle(Component):
    """``rectangle``: props ``fill`` (bool) and ``outline_thickness`` (0..1000)."""
    __slots__ = ()


class Line(Component):
    """``line``: props ``line_width`` (0..1000) and ``line_invert`` (bool)."""
    __slots__ = ()


class Text(Component):
    """``text``: prop ``text`` (under 1024 bytes of UTF-8)."""

    __slots__ = ()

    def set_text(self, text: str) -> None:
        self.set(text=text)

    def read_text(self) -> str | None:
        """The text the client holds now (``ui.get`` with ``content``)."""
        return self.get(content=True).content


class Canvas(Component):
    """``canvas``: draw on it with :meth:`draw`."""

    __slots__ = ()

    def draw(self) -> "CanvasBatch":
        """A batch of canvas ops, sent with one ``ui.canvas`` call (per 128 ops) by
        :meth:`CanvasBatch.submit`, or on leaving a ``with`` block without an exception."""
        return CanvasBatch(self)

    def clear(self) -> int:
        return self.draw().clear().submit()

    def undo(self) -> int:
        return self.draw().undo().submit()

    def _submit(self, ops: list[dict[str, Any]]) -> int:
        return int(self._ui._call("canvas", self.handle, ops))


class CanvasBatch:
    """Canvas ops queued for one submit. Each op method returns the batch, so they chain:
    ``canvas.draw().rect(...).line(...).submit()``. Colours are ``0xRRGGBBAA`` (see :func:`rgba`).
    """

    __slots__ = ("_canvas", "_ops", "elements", "sent", "_submitted")

    def __init__(self, canvas: Canvas) -> None:
        self._canvas = canvas
        self._ops: list[dict[str, Any]] = []
        self.elements: int | None = None   # the canvas's element count after the submit
        self.sent = 0                      # ops the agent applied: whole chunks that succeeded
        self._submitted = False

    def _add(self, op: str, **args: int) -> "CanvasBatch":
        self._ops.append({"op": op, **args})
        return self

    def line(self, x: int, y: int, end_x: int, end_y: int, *, rgba: int,
             thickness: int = 1) -> "CanvasBatch":
        return self._add("line", x=x, y=y, end_x=end_x, end_y=end_y, thickness=thickness,
                         rgba=rgba)

    def outline(self, x: int, y: int, width: int, height: int, *, rgba: int,
                thickness: int = 1) -> "CanvasBatch":
        return self._add("outline", x=x, y=y, width=width, height=height, thickness=thickness,
                         rgba=rgba)

    def rect(self, x: int, y: int, width: int, height: int, *, rgba: int) -> "CanvasBatch":
        return self._add("rect", x=x, y=y, width=width, height=height, rgba=rgba)

    def gradient_h(self, x: int, y: int, width: int, height: int, *, rgba_start: int,
                   rgba_end: int) -> "CanvasBatch":
        return self._add("gradient_h", x=x, y=y, width=width, height=height,
                         rgba_start=rgba_start, rgba_end=rgba_end)

    def gradient_v(self, x: int, y: int, width: int, height: int, *, rgba_start: int,
                   rgba_end: int) -> "CanvasBatch":
        return self._add("gradient_v", x=x, y=y, width=width, height=height,
                         rgba_start=rgba_start, rgba_end=rgba_end)

    def triangle(self, x0: int, y0: int, x1: int, y1: int, x2: int, y2: int, *,
                 rgba: int) -> "CanvasBatch":
        return self._add("triangle", x0=x0, y0=y0, x1=x1, y1=y1, x2=x2, y2=y2, rgba=rgba)

    def clear(self) -> "CanvasBatch":
        return self._add("clear")

    def undo(self) -> "CanvasBatch":
        return self._add("undo")

    def op(self, name: str, **args: int) -> "CanvasBatch":
        """Any op by name, for one the agent adds later."""
        return self._add(name, **args)

    def __len__(self) -> int:
        return len(self._ops)

    def submit(self) -> int:
        """Send the queued ops, in order, in chunks of :data:`CANVAS_OPS_MAX`; returns the
        canvas's element count. An empty batch sends nothing (and returns -1).

        **A batch of more than 128 ops is not atomic.** Each chunk is one ``ui.canvas`` call,
        which the agent applies all or nothing; a refused chunk raises as the host raised it,
        and the chunks after it are not sent, but the chunks before it stay drawn. ``sent`` is
        then how many ops were applied, and the exception carries a note saying so."""
        if self._submitted:
            raise RuntimeError("this canvas batch was already submitted")
        self._submitted = True
        if not self._ops:
            return -1
        for start in range(0, len(self._ops), CANVAS_OPS_MAX):
            chunk = self._ops[start:start + CANVAS_OPS_MAX]
            try:
                self.elements = self._canvas._submit(chunk)
            except Exception as exc:
                exc.add_note(f"canvas batch: {self.sent} of {len(self._ops)} ops were applied "
                             f"before this chunk failed (batch.sent)")
                raise
            self.sent += len(chunk)
        return self.elements if self.elements is not None else -1

    def __enter__(self) -> "CanvasBatch":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None and not self._submitted:
            self.submit()


class Button(Component):
    """``button``: props ``label`` (under 1024 bytes), ``can_toggle``, ``toggled`` (needs
    ``can_toggle``). Hook ``onbuttonpressed``: :meth:`on_pressed`."""

    __slots__ = ()

    def set_label(self, label: str) -> None:
        self.set(label=label)

    def set_toggled(self, toggled: bool) -> None:
        self.set(toggled=bool(toggled))

    def on_pressed(self, handler: Callable[[UiButtonPressed], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONBUTTONPRESSED, handler, UiButtonPressed)


class Check(Component):
    """``check``: prop ``checked``. The agent refuses a check ``label`` (not supported yet).
    Hook ``onbuttonpressed`` (``current`` is the new checked state): :meth:`on_pressed`."""

    __slots__ = ()

    def set_checked(self, checked: bool) -> None:
        self.set(checked=bool(checked))

    def is_checked(self) -> bool:
        return bool(self.get().checked)

    def on_pressed(self, handler: Callable[[UiButtonPressed], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONBUTTONPRESSED, handler, UiButtonPressed)


class InputField(Component):
    """``input_field``: props ``content`` (under 1024 bytes), ``filter`` (``none``,
    ``alphanumeric``, ``numeric``, ``quantity``, ``numbers_as_text``), ``max_length``
    (1..9999). Hook ``oncontentchanged``: :meth:`on_content_changed`; a content set raises it
    too. **While focused, the game's keys go to the field**: :meth:`blur` it when done.

    On 951-4 it may not draw at all (``get().renders`` False): see :class:`UiReadback`."""

    __slots__ = ()

    def set_content(self, content: str) -> None:
        self.set(content=content)

    def read_content(self) -> str | None:
        """The text the client holds now (the client may have truncated or filtered a set)."""
        return self.get(content=True).content

    def blur(self) -> bool:
        """``ui.blur``: drop the field's input focus. True if it had focus."""
        return bool(self._ui._call("blur", self.handle))

    def on_content_changed(self, handler: Callable[[UiContentChanged], None]
                           ) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONCONTENTCHANGED, handler, UiContentChanged)


class Slider(Component):
    """``slider``: props ``min``, ``max``, ``step``, ``ticks`` (all four together:
    :meth:`set_range`) and ``value``. Hook ``onselectionchanged``: :meth:`on_changed` (the
    user's moves only; a value set raises nothing)."""

    __slots__ = ()

    def set_range(self, min_value: int, max_value: int, step: int, ticks: int, *,
                  value: int | None = None) -> None:
        """The client's rule: ``min < max``, ``0 < step <= max - min``, ``(max - min) % step ==
        0``, ``0 < ticks <= max - min``. The value resets to ``min`` unless ``value`` is given."""
        props: dict[str, Any] = {"min": min_value, "max": max_value, "step": step, "ticks": ticks}
        if value is not None:
            props["value"] = value
        self.set(**props)

    def set_value(self, value: int) -> None:
        """``min <= value <= max`` and ``value % step == 0``, or the agent refuses it."""
        self.set(value=value)

    def read_value(self) -> int:
        """The slider's value now. Raises ``RuntimeError`` if the readback has none (not a
        slider, or a host without the P2 readback)."""
        value = self.get().value
        if value is None:
            raise RuntimeError(f"ui: component {self.handle} read back no slider value")
        return int(value)

    def on_changed(self, handler: Callable[[UiSliderChanged], None]) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONSELECTIONCHANGED, handler, UiSliderChanged)


class ComboField(Component):
    """``combo_field``: props ``entries`` (replaces every entry; at most 256, unique ids) and
    ``selected`` (an entry id, -1 none). Hook ``onselectionchanged``:
    :meth:`on_selection_changed` (the user's picks only; a set raises nothing)."""

    __slots__ = ()

    def set_entries(self, entries: Iterable[EntryLike]) -> None:
        self.set(entries=list(entries))

    def select(self, entry_id: int | None) -> None:
        """Select ``entry_id``; None selects nothing."""
        self.set(selected=-1 if entry_id is None else int(entry_id))

    def read_selected(self) -> int | None:
        selected = self.get().selected
        return None if selected is None or selected == NO_SELECTION else int(selected)

    def on_selection_changed(self, handler: Callable[[UiEntrySelectionChanged], None]
                             ) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONSELECTIONCHANGED, handler,
                            UiEntrySelectionChanged)


class ListBox(Component):
    """``list`` (made with ``ctx.ui.list``): props ``entries``, ``selected`` (the ids that end
    up selected) and ``selection_limit`` (1..255; 1 is single select, and a limit L > 1 holds
    at most L - 1 entries on 951-4). Hook ``onselectionchanged``: :meth:`on_selection_changed`
    (the user's picks only; a set raises nothing)."""

    __slots__ = ()

    def set_entries(self, entries: Iterable[EntryLike]) -> None:
        self.set(entries=list(entries))

    def select(self, entry_ids: Iterable[int]) -> None:
        """Exactly ``entry_ids`` end up selected (none for an empty iterable)."""
        self.set(selected=[int(i) for i in entry_ids])

    def read_selected(self) -> tuple[int, ...]:
        selected = self.get().selected
        return tuple(selected) if isinstance(selected, tuple) else ()

    def set_selection_limit(self, limit: int) -> None:
        self.set(selection_limit=limit)

    def on_selection_changed(self, handler: Callable[[UiEntrySelectionChanged], None]
                             ) -> Callable[[], None]:
        return self._ui._on(self.handle, UiHook.ONSELECTIONCHANGED, handler,
                            UiEntrySelectionChanged)


class Panel(Component):
    """``panel``: a container that lays its children out; prop ``vertical``. The agent refuses
    ``spacing`` (not supported yet)."""

    __slots__ = ()

    def set_vertical(self, vertical: bool) -> None:
        self.set(vertical=bool(vertical))


class Grid(Component):
    """``grid``: a container. The agent refuses its layout props (not supported yet)."""
    __slots__ = ()


NO_SELECTION = -1   # a combo_field's ``selected`` when nothing is

# The wrapper class per type name. A type not listed here comes back as a plain Component.
_WRAPPERS: dict[str, type[Component]] = {
    ComponentType.CANVAS.value: Canvas,
    ComponentType.RECTANGLE.value: Rectangle,
    ComponentType.LINE.value: Line,
    ComponentType.TEXT.value: Text,
    ComponentType.LAYER.value: Layer,
    ComponentType.BUTTON.value: Button,
    ComponentType.CHECK.value: Check,
    ComponentType.INPUT_FIELD.value: InputField,
    ComponentType.SLIDER.value: Slider,
    ComponentType.COMBO_FIELD.value: ComboField,
    ComponentType.LIST.value: ListBox,
    ComponentType.PANEL.value: Panel,
    ComponentType.GRID.value: Grid,
}

Parent = Union[_Handle, int]
UiHandler = Callable[[UiEvent], None]

_UNKNOWN = "unknown handle"   # in the agent's refusal for a handle it no longer has


class _Sub:
    """One handler of a script's ui events: filtered by ``handle``, ``hook`` and event class
    ``kind`` when given. ``wired``: it holds a ``ui.subscribe`` (made by ``Component.on``)."""

    __slots__ = ("handler", "handle", "hook", "kind", "wired")

    def __init__(self, handler: Callable[[Any], None], handle: int | None, hook: str | None,
                 kind: type[UiEvent] | None = None, wired: bool = False) -> None:
        self.handler = handler
        self.handle = handle
        self.hook = hook
        self.kind = kind
        self.wired = wired

    def wants(self, event: UiEvent) -> bool:
        return ((self.handle is None or self.handle == event.handle)
                and (self.hook is None or self.hook == event.hook)
                and (self.kind is None or isinstance(event, self.kind)))


def _given(props: dict[str, Any], **named: Any) -> dict[str, Any]:
    """``props`` plus the named arguments that were given (not None), named ones first."""
    return {**{k: v for k, v in named.items() if v is not None}, **props}


def _parent_handle(parent: Parent) -> int:
    if isinstance(parent, _Handle):
        return parent.handle
    if isinstance(parent, int) and not isinstance(parent, bool):
        return parent
    raise TypeError(f"ui: a parent is a component or a handle, not {type(parent).__name__}")


# Events for a handle no script holds yet: one can race the create (or lookup) that returns
# the handle. Handles are never reused, so keeping them briefly is safe. The same TTL keeps a
# tombstone for a handle whose ``destroyed`` was delivered, for a script that claims a shared
# Jagex handle just after.
PENDING_MAX = 256
PENDING_TTL_S = 10.0

# What a ui call raises once the script's plugin UI was released (the script stopped): a
# component made then would be tracked by no one and freed only when the connection closes.
STOPPED = "ui: this script has stopped"

# UiRouter.claim results.
CLAIM_HELD = "held"        # the scope holds the handle now
CLAIM_GONE = "gone"        # already destroyed: not held; its destroyed event goes to the scope
CLAIM_CLOSED = "closed"    # the scope was released meanwhile: not held


class UiRouter:
    """One per client connection: every script's :class:`PluginUi`, and the ui events not yet
    claimed. One lock covers who holds which handle, so a create that inserts its handle and a
    poll that routes an event for it cannot miss each other: either the poll sees the handle
    held, or the event is pending when the create claims it.

    Routing (:meth:`route`): an event for a component goes to the script that made it; for a
    Jagex handle, to every script that looked it up (the agent dedups lookups per connection,
    so scripts share it); ``overflow`` (handle 0) to every live UI script. An event no script
    holds is kept for :data:`PENDING_TTL_S` (at most :data:`PENDING_MAX`), and delivered by the
    next route after a script claims its handle. A delivered ``destroyed`` leaves a tombstone for
    as long: a script claiming that handle afterwards (a Jagex lookup answered with a handle
    another script already holds) does not hold it, and gets the event too."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.lock = threading.RLock()
        self._clock = clock or time.monotonic
        self._scopes: list[PluginUi] = []
        self._pending: dict[int, list[tuple[float, UiEvent]]] = {}
        self._tombstones: dict[int, tuple[float, UiEvent]] = {}
        # Events for the next route: (event, None) is routed to the handle's holders,
        # (event, [scope]) goes to those scopes as it is.
        self._ready: list[tuple[UiEvent, list[PluginUi] | None]] = []

    def add(self, scope: "PluginUi") -> None:
        with self.lock:
            self._scopes = [s for s in self._scopes if not s.closed] + [scope]

    def delivering_here(self) -> bool:
        """True while this thread is running a ui handler of any script on this connection."""
        with self.lock:
            scopes = list(self._scopes)
        return any(s.delivering_here() for s in scopes)

    def live(self) -> list["PluginUi"]:
        """The scopes that have used the UI and are not released."""
        with self.lock:
            return [s for s in self._scopes if s.used and not s.closed]

    def claim(self, scope: "PluginUi", handle: int, jagex: bool) -> str:
        """``scope`` takes ``handle``, which a call just returned. :data:`CLAIM_HELD`: it holds it
        now, and any event that arrived for it first is queued for the next :meth:`route`.
        :data:`CLAIM_GONE`: its ``destroyed`` was already delivered (a tombstone); not held, and
        that event is queued for ``scope``. :data:`CLAIM_CLOSED`: ``scope`` was released."""
        with self.lock:
            if scope.closed:
                return CLAIM_CLOSED
            tomb = self._tombstones.get(handle)
            if tomb is not None:
                self._ready.append((tomb[1], [scope]))
                return CLAIM_GONE
            scope._hold(handle, jagex)
            early = self._pending.pop(handle, None)
            if early:
                self._ready.extend((event, None) for _, event in early)
            return CLAIM_HELD

    def pending_count(self) -> int:
        with self.lock:
            return sum(len(v) for v in self._pending.values())

    def route(self, raw_events: Iterable[Mapping[str, Any]] = ()) -> int:
        """Route the claimed early events, then ``raw_events`` (``ui_poll_events`` dicts).
        Ownership changes under the lock; handlers run after it. Returns the deliveries."""
        parsed: list[tuple[UiEvent, list[PluginUi] | None]] = []
        for raw in raw_events:
            try:
                parsed.append((UiEvent.from_native(raw), None))
            except Exception:   # one bad event: skip it, keep the rest of the batch
                _log.exception("ui: skipping a malformed ui event: %r", raw)
        with self.lock:
            self._expire()
            events, self._ready = self._ready + parsed, []
            plan = [(event, fixed if fixed is not None else self._targets(event))
                    for event, fixed in events]
        delivered = 0
        for event, targets in plan:
            for scope in targets:
                scope._deliver(event)
                delivered += 1
        return delivered

    def _targets(self, event: UiEvent) -> list["PluginUi"]:
        live = [s for s in self._scopes if s.used and not s.closed]
        if event.handle == 0:
            return live
        targets = [s for s in live if s._holds(event.handle)]
        if not targets:
            self._keep(event)
        elif event.is_destroyed:
            for scope in targets:
                scope._drop(event.handle)
            self._tombstones[event.handle] = (self._clock(), event)
        return targets

    def _keep(self, event: UiEvent) -> None:
        self._pending.setdefault(event.handle, []).append((self._clock(), event))
        total = sum(len(v) for v in self._pending.values())
        while total > PENDING_MAX:
            oldest = min(self._pending, key=lambda h: self._pending[h][0][0])
            self._pending[oldest].pop(0)
            if not self._pending[oldest]:
                del self._pending[oldest]
            total -= 1

    def _expire(self) -> None:
        cutoff = self._clock() - PENDING_TTL_S
        for handle in list(self._pending):
            kept = [(t, e) for t, e in self._pending[handle] if t >= cutoff]
            if kept:
                self._pending[handle] = kept
            else:
                del self._pending[handle]
        for handle in [h for h, (t, _) in self._tombstones.items() if t < cutoff]:
            del self._tombstones[handle]


class PluginUi:
    """One script's plugin UI (``ctx.ui``): what it creates, and its events.

    Tracks the handles of the components this script made, so that :meth:`release` (run by the
    framework when the script stops) destroys exactly those and no other script's, and the Jagex
    handles it looked up, so their events reach it. Thread-safe: the script's thread makes
    components while the client's ui event thread routes events here."""

    def __init__(self, host: "NativeHost", on_used: Callable[[], None] | None = None,
                 router: UiRouter | None = None) -> None:
        self._host = host
        self._on_used = on_used
        self._router = router or UiRouter()
        self._lock = self._router.lock       # one lock per client: see UiRouter
        self._owned: dict[int, None] = {}    # creation order: release goes newest first
        self._jagex: set[int] = set()        # Jagex handles looked up, not yet destroyed
        self._hooks: dict[int, set[str]] = {}  # handle -> hooks subscribed on the agent
        self._handlers: list[_Sub] = []
        self._events_bus: Any = None         # the script's ScriptEventBus, set by the runtime
        self._closed = False                 # released: nothing is routed here any more
        # Delivery: closed when the script stops (before on_stop); the predicate is the
        # script's own stop request, set by the runtime, so delivery ends as soon as it is asked.
        self._delivery_lock = threading.RLock()
        self._delivery_closed = False
        self._stop_requested: Callable[[], bool] | None = None
        self._delivering: threading.Thread | None = None   # the thread in _deliver, if any
        self._router.add(self)

    # --- verbs -----------------------------------------------------------------

    def component(self, iface: int, comp: int, key: int = KEY_STATIC) -> JagexComponent:
        """``ui.get_component``: a Jagex component, as a parent for :meth:`create`. The agent
        answers every script on one client with the same handle for the same component."""
        found = JagexComponent(self, self._call("get_component", int(iface), int(comp), int(key)))
        if self._router.claim(self, found.handle, jagex=True) == CLAIM_CLOSED:
            raise RuntimeError(STOPPED)   # a lookup, not a component: nothing to free
        return found

    def create(self, parent: Parent, type: str | ComponentType, *,
               category: int | None = None, dynamic: int | None = None,
               **props: Any) -> Component:
        """``ui.create``: a ``type`` component under ``parent`` with ``props`` applied.

        ``category`` (1..234) and ``dynamic`` (0..4095) default to the agent's choice (the
        parent's category, its next free dynamic id). ``type`` is a :class:`ComponentType` or
        any type name; the result is that type's wrapper class, or :class:`Component`."""
        name = type.value if isinstance(type, ComponentType) else str(type)
        info = self._call("create", _parent_handle(parent), name,
                          _wire_props(props) if props else None, category, dynamic)
        wrapper = (_WRAPPERS.get(name) or _WRAPPERS.get(str(info.get("type_name", "")))
                   or Component)
        made = wrapper(self, info)
        if self._router.claim(self, made.handle, jagex=False) == CLAIM_CLOSED:
            # Released while the create was in flight: free it now, or it stays up untracked
            # for as long as the connection does.
            try:
                self._host.ui("destroy", made.handle)
            except Exception as exc:  # noqa: BLE001 - the host's release at detach is the backstop
                _log.info("ui: could not free component %s made after the script stopped: %s",
                          made.handle, exc)
            raise RuntimeError(STOPPED)
        return made

    def layer(self, parent: Parent, **props: Any) -> Layer:
        return self._typed(parent, ComponentType.LAYER, props)

    def text(self, parent: Parent, text: str, **props: Any) -> Text:
        return self._typed(parent, ComponentType.TEXT, {"text": text, **props})

    def rectangle(self, parent: Parent, **props: Any) -> Rectangle:
        return self._typed(parent, ComponentType.RECTANGLE, props)

    def line(self, parent: Parent, **props: Any) -> Line:
        return self._typed(parent, ComponentType.LINE, props)

    def canvas(self, parent: Parent, **props: Any) -> Canvas:
        return self._typed(parent, ComponentType.CANVAS, props)

    # P2: the interactive types. Each helper's named arguments are sent only when given, so the
    # agent's own defaults apply otherwise.

    def button(self, parent: Parent, label: str | None = None, **props: Any) -> Button:
        return self._typed(parent, ComponentType.BUTTON, _given(props, label=label))

    def check(self, parent: Parent, checked: bool | None = None, **props: Any) -> Check:
        return self._typed(parent, ComponentType.CHECK, _given(props, checked=checked))

    def input_field(self, parent: Parent, content: str | None = None, **props: Any
                    ) -> InputField:
        """An input_field. Expect ``get().renders`` False on 951-4 (see :class:`UiReadback`)."""
        return self._typed(parent, ComponentType.INPUT_FIELD, _given(props, content=content))

    def slider(self, parent: Parent, **props: Any) -> Slider:
        """A slider. ``min``, ``max``, ``step`` and ``ticks`` go together (all or none);
        ``value`` is optional."""
        return self._typed(parent, ComponentType.SLIDER, props)

    def combo_field(self, parent: Parent, entries: Iterable[EntryLike] | None = None,
                    **props: Any) -> ComboField:
        return self._typed(parent, ComponentType.COMBO_FIELD,
                           _given(props, entries=None if entries is None else list(entries)))

    def list(self, parent: Parent, entries: Iterable[EntryLike] | None = None,
             **props: Any) -> ListBox:
        """A ``list`` component (:class:`ListBox`). The listing verb is :meth:`handles`."""
        return self._typed(parent, ComponentType.LIST,
                           _given(props, entries=None if entries is None else [*entries]))

    def panel(self, parent: Parent, vertical: bool | None = None, **props: Any) -> Panel:
        return self._typed(parent, ComponentType.PANEL, _given(props, vertical=vertical))

    def grid(self, parent: Parent, **props: Any) -> Grid:
        return self._typed(parent, ComponentType.GRID, props)

    @property
    def has_p2(self) -> bool:
        """Whether this bwu_host has the P2 surface (hooks, blur, the interactive props). Says
        nothing about the agent: an agent before P2 refuses the interactive types verbatim."""
        return self._host.has_ui_p2

    def handles(self) -> "list[dict[str, Any]]":
        """``ui.list``: every handle on this script's connection, quarantined ones included.
        On a client shared with other scripts, that includes theirs. (Named ``handles``, not
        ``list``, so ``list`` stays free for the agent's ``list`` component type.)"""
        return list(self._call("list"))

    def owned(self) -> list[int]:
        """The handles of this script's live components, oldest first."""
        with self._lock:
            return list(self._owned)

    # --- events ----------------------------------------------------------------

    def subscribe(self, handler: UiHandler, *, handle: int | None = None,
                  hook: str | None = None) -> Callable[[], None]:
        """``handler`` for this script's :class:`UiEvent` events, only those for ``handle``
        and/or ``hook`` when given. Returns an unsubscribe callable. Runs on the client's ui
        event thread: keep it cheap; an exception from it is logged and swallowed."""
        entry = _Sub(handler, handle, hook)
        with self._lock:
            self._handlers.append(entry)

        def _off() -> None:
            with self._lock:
                if entry in self._handlers:
                    self._handlers.remove(entry)

        return _off

    def _on(self, handle: int, hook: str, handler: Callable[[Any], None],
            kind: type[UiEvent] | None) -> Callable[[], None]:
        """``Component.on`` and its typed helpers: the handler first (so an event racing the
        subscribe's reply is not missed), then ``ui.subscribe``. ``kind``: deliver only events
        of that class (a typed helper never hands its handler an undecodable event)."""
        entry = _Sub(handler, handle, hook, kind, wired=hook != UiHook.DESTROYED)
        with self._lock:
            self._handlers.append(entry)
        if entry.wired:
            try:
                self._subscribe_hook(handle, hook)
            except BaseException:
                with self._lock:
                    self._handlers.remove(entry)
                raise

        def _off() -> None:
            with self._lock:
                if entry not in self._handlers:
                    return
                self._handlers.remove(entry)
                last = entry.wired and not any(
                    s.wired and s.handle == handle and s.hook == hook for s in self._handlers)
            if last:
                try:
                    self._unsubscribe_hook(handle, hook)
                except Exception as exc:  # noqa: BLE001 - off() never raises
                    _log.info("ui: unsubscribing %s on %s failed: %s", hook, handle, exc)

        return _off

    def _subscribe_hook(self, handle: int, hook: str) -> None:
        self._call("subscribe", int(handle), str(hook))
        with self._lock:
            if handle in self._owned:   # not if a destroyed event raced the reply
                self._hooks.setdefault(handle, set()).add(str(hook))

    def _unsubscribe_hook(self, handle: int, hook: str) -> bool:
        with self._lock:
            gone = self._closed or handle not in self._owned
            if gone:
                self._hooks.pop(handle, None)
        if gone:
            return False   # the agent dropped its subscriptions with the component
        refused = self._host.ui_refused_type()
        try:
            was = bool(self._call("unsubscribe", int(handle), str(hook)))
        except Exception as exc:
            if refused is not None and isinstance(exc, refused) and _UNKNOWN in str(exc):
                was = False   # gone on the agent's side: a success, as the host's own rule
            else:
                raise
        with self._lock:
            hooks = self._hooks.get(handle)
            if hooks is not None:
                hooks.discard(str(hook))
                if not hooks:
                    del self._hooks[handle]
        return was

    def _hooks_of(self, handle: int) -> frozenset[str]:
        with self._lock:
            return frozenset(self._hooks.get(handle, ()))

    def _deliver(self, event: UiEvent) -> None:
        """Called by the router, outside its lock, with an event for this script.

        Runs under the scope's delivery lock, so :meth:`close_delivery` (before ``on_stop``)
        waits for it. The plan the router made can go stale while earlier handlers run: one can
        destroy the component, stop the script or remove another handler. So the conditions are
        checked again before **each** handler and before the bus: delivery still open (no stop
        requested), the handle still held (a ``destroyed`` event, a tombstone delivery and
        ``overflow`` are exempt: their handle is dropped or 0 by design), and the handler still
        subscribed."""
        with self._delivery_lock:
            self._delivering = threading.current_thread()
            try:
                self._deliver_locked(event)
            finally:
                self._delivering = None

    def _deliver_locked(self, event: UiEvent) -> None:
        """:meth:`_deliver`'s body; the caller holds ``_delivery_lock``."""
        with self._lock:
            if not self._open_for(event):
                return
            subs = [s for s in self._handlers if s.wants(event)]
            bus = self._events_bus
        for sub in subs:
            with self._lock:
                if not self._open_for(event):
                    return
                if sub not in self._handlers:
                    continue   # off() or unsubscribe_hook since the snapshot
            try:
                sub.handler(event)
            except Exception:
                _log.exception("ui event handler failed")
        with self._lock:
            if bus is None or not self._open_for(event):
                return
        bus.publish(event)

    def _open_for(self, event: UiEvent) -> bool:
        """Caller holds ``_lock``: may ``event`` still be delivered to this script?"""
        if self._delivery_closed or self._closed:
            return False
        stop = self._stop_requested
        if stop is not None and stop():
            return False
        if event.is_destroyed or event.handle == 0:
            return True
        return self._holds(event.handle)

    def close_delivery(self) -> None:
        """No ui event is delivered to this script from now on. Waits for a delivery in flight
        to finish first, so no handler runs during or after ``on_stop``. Run by the framework
        when the script stops, before ``on_stop``; idempotent. (A handler that stops the script
        and then waits for it to finish would wait for itself: :meth:`ScriptRunner.stop` and
        ``Connection.close`` therefore never wait on the script from inside its own delivery.)"""
        with self._lock:
            # First, so a delivery in flight skips the rest of its handlers (they re-check).
            self._delivery_closed = True
        with self._delivery_lock:
            pass   # wait for the delivery in flight, if any, to return

    def delivering_here(self) -> bool:
        """True while this thread is running one of this script's ui handlers."""
        return self._delivering is threading.current_thread()

    # The router calls these three with its lock held (claim checks closed first).
    def _hold(self, handle: int, jagex: bool) -> None:
        if jagex:
            self._jagex.add(handle)
        else:
            self._owned[handle] = None

    def _holds(self, handle: int) -> bool:
        return handle in self._owned or handle in self._jagex

    def _drop(self, handle: int) -> None:
        self._owned.pop(handle, None)
        self._jagex.discard(handle)
        self._hooks.pop(handle, None)   # the agent dropped them with it: nothing to send

    def _owns(self, handle: int) -> bool:
        with self._lock:
            return handle in self._owned

    def _holds_jagex(self, handle: int) -> bool:
        with self._lock:
            return handle in self._jagex

    @property
    def used(self) -> bool:
        """True once this script made a ui call (its events are then polled)."""
        return self._used

    @property
    def closed(self) -> bool:
        """True once :meth:`release` ran (the script stopped)."""
        return self._closed

    # --- release ---------------------------------------------------------------

    def release(self) -> int:
        """Destroy every component this script still owns, newest first, with ``ui.destroy``.
        Run by the framework after ``on_stop``; idempotent. Never ``ui.destroy_all``: another
        script on the same client keeps its components. Jagex handles are lookups, not
        components: they are forgotten here, and the host releases them at detach.

        Best effort, and it never raises: a handle the agent no longer knows (a parent's
        destroy took it, or a ``destroyed`` event raced the release) is refused and skipped. A
        timeout or a lost client ends the release; the host's release at detach, and the agent's
        when the pipe closes, take whatever is left. Returns how many were destroyed."""
        self.close_delivery()
        with self._lock:
            self._handlers.clear()
            handles = list(reversed(self._owned))
            self._owned.clear()
            self._jagex.clear()
            self._hooks.clear()   # ui.destroy takes each component's subscriptions with it
            self._closed = True
        destroyed = 0
        refused = self._host.ui_refused_type()
        for handle in handles:
            try:
                self._host.ui("destroy", handle)
                destroyed += 1
            except Exception as exc:  # noqa: BLE001 - the script is going away
                if refused is not None and isinstance(exc, refused):
                    _log.debug("ui release: handle %s already gone: %s", handle, exc)
                    continue
                _log.info("ui release stopped at handle %s (%s: %s); the host's release "
                          "takes the rest", handle, type(exc).__name__, exc)
                break
        return destroyed

    # --- internals -------------------------------------------------------------

    _used = False

    def _typed(self, parent: Parent, kind: ComponentType, props: dict[str, Any]) -> Any:
        return self.create(parent, kind, **props)

    def _call(self, verb: str, *args: Any) -> Any:
        if self._closed:
            raise RuntimeError(STOPPED)
        if not self._used:
            self._used = True
            if self._on_used is not None:
                self._on_used()
        return self._host.ui(verb, *args)

    def _destroy(self, handle: int) -> bool:
        refused = self._host.ui_refused_type()
        try:
            freed = bool(self._call("destroy", handle))
        except Exception as exc:
            # The host forgets the handle on any answer from the agent, a refusal included; so
            # does the script. A timeout or a lost pipe leaves it owned, for release to retry.
            if refused is not None and isinstance(exc, refused):
                self._forget(handle)
            raise
        self._forget(handle)
        return freed

    def _forget(self, handle: int) -> None:
        with self._lock:
            self._owned.pop(handle, None)
            self._hooks.pop(handle, None)


def __getattr__(name: str) -> Any:
    """``UiRefused``: the native host's own exception class (``botwithus._native.UiRefused``),
    so ``except UiRefused`` catches exactly what the host raises."""
    if name == "UiRefused":
        from ..core import native
        refused = getattr(native.backend(), "UiRefused", None)
        if refused is None:
            raise AttributeError("this bwu_host has no UiRefused; it predates the plugin UI")
        return refused
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
