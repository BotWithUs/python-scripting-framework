"""Typed game events and a small synchronous event bus.

Events are decoded from the shared-memory event ring (see ``core.events``). Each is an
immutable dataclass. Unknown event types are skipped by the decoder, never delivered.

The ``EventBus`` dispatches inline on the calling (pump) thread, keyed by event class.
Handlers must be cheap and must not raise; a raising handler is swallowed so it can't
stall the pump or drop sibling handlers.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Callable, TypeVar

_log = logging.getLogger("botwithus.events")

@dataclass(frozen=True, slots=True)
class GameEvent:
    """Base for all ring events. ``seq`` is the ring sequence number."""
    seq: int


@dataclass(frozen=True, slots=True)
class LoginStateChanged(GameEvent):
    old_state: int
    new_state: int


@dataclass(frozen=True, slots=True)
class ServerTick(GameEvent):
    tick: int


@dataclass(frozen=True, slots=True)
class VarpChanged(GameEvent):
    id: int
    old_value: int
    new_value: int


@dataclass(frozen=True, slots=True)
class VarbitChanged(GameEvent):
    id: int
    old_value: int
    new_value: int


@dataclass(frozen=True, slots=True)
class VarcChanged(GameEvent):
    id: int
    old_value: int
    new_value: int


@dataclass(frozen=True, slots=True)
class ObjVarChanged(GameEvent):
    """A per-item obj var on one inventory slot changed (event 13, ``ObjVarChangeBody``).

    Obj vars live on the item instance in a slot (augmentation XP, charges and the like).
    ``inv_id`` and ``slot`` say which slot; correlate it with ``SnapshotView.inventory``.
    ``GameApi.obj_vars`` reads the current values on demand."""
    inv_id: int
    slot: int
    var_id: int
    old_value: int
    new_value: int


@dataclass(frozen=True, slots=True)
class ChatMessage(GameEvent):
    msg_type: int
    sender: str
    text: str


@dataclass(frozen=True, slots=True)
class KeyInput(GameEvent):
    """One key-down, as the game's input dispatcher received it. Key-up is not reported.

    ``key`` is the Windows virtual-key code the game received: the generic ``VK_SHIFT`` (16),
    ``VK_CONTROL`` (17) and ``VK_MENU`` (18), never the left/right variants. ``is_alt``,
    ``is_ctrl`` and ``is_shift`` are the modifiers held, including the pressed key itself.

    ``synthetic``: the agent's own ``send_key`` sent this key, not the user.

    ``text_input_focused``: a text field had the keyboard at the moment this key-down
    arrived: the chat line, bank search, a game prompt, the bug-report box, or a native
    input/list field. Scripts can use it to ignore keys typed into text. Known gap: the
    effect of the "always-on chat" setting is not established.

    ``is_repeat``: autorepeat of a key that is already held. Not set on autorepeats while
    the developer console is open; normal play is unaffected.

    An older agent sends no flags, so ``synthetic``, ``text_input_focused`` and ``is_repeat``
    all read ``False`` against it.
    """
    key: int
    is_alt: bool
    is_ctrl: bool
    is_shift: bool
    synthetic: bool = False
    text_input_focused: bool = False
    is_repeat: bool = False


@dataclass(frozen=True, slots=True)
class ActionExecuted(GameEvent):
    action_id: int
    param1: int
    param2: int
    param3: int


class RefuseReason(IntEnum):
    """Why the agent dropped a queued action (``ActionRefusedBody.reason``). Values are
    permanent on the agent's side."""
    NO_TARGET = 1        # the target does not exist, or the action has none a click could land on
    ANCHOR_TIMEOUT = 2   # the target never became clickable in time
    NO_OPENER = 3        # it stayed hidden behind a window or tab the agent cannot open


class AnchorCase(IntEnum):
    """What the agent was waiting on while it held the action (``ActionRefusedBody.anchorCase``)."""
    NONE = 0
    SCROLL = 1
    OPEN = 2
    CAMERA = 3
    WAIT = 4             # a transient miss


@dataclass(frozen=True, slots=True)
class ActionRefused(GameEvent):
    """A queued action the agent dropped without dispatching it, because its target never
    became clickable. No :class:`ActionExecuted` follows it.

    The action is as it was queued. ``reason`` and ``anchor_case`` are a :class:`RefuseReason`
    and an :class:`AnchorCase`, or the raw int for a value this package does not know yet (an
    IntEnum compares equal to its int, so ``reason == 2`` works either way). ``held_ms`` is how
    long the agent held it first, not counting blocked or break time."""
    action_id: int
    param1: int
    param2: int
    param3: int
    reason: RefuseReason | int
    anchor_case: AnchorCase | int
    held_ms: int


@dataclass(frozen=True, slots=True)
class BreakStarted(GameEvent):
    duration_seconds: int
    fatigue: float
    risk: float


@dataclass(frozen=True, slots=True)
class BreakEnded(GameEvent):
    pass


@dataclass(frozen=True, slots=True)
class Hitmark(GameEvent):
    target_server_index: int
    target_type: int
    hitmark_type: int
    damage: int
    cycle: int


@dataclass(frozen=True, slots=True)
class Headbar(GameEvent):
    target_server_index: int
    target_type: int
    headbar_type: int
    value: int


@dataclass(frozen=True, slots=True)
class InventoryChanged(GameEvent):
    """One inventory slot changed between two server ticks. Derived by the framework from the
    snapshot (not a ring event), so ``seq`` is -1. The Java host's ``InventoryChangeEvent``."""
    inv_id: int
    slot: int
    old_item: Any        # InventoryItem
    new_item: Any        # InventoryItem
    server_tick: int

    @property
    def is_added(self) -> bool:
        return self.old_item.empty and not self.new_item.empty

    @property
    def is_removed(self) -> bool:
        return not self.old_item.empty and self.new_item.empty

    @property
    def quantity_delta(self) -> int:
        if self.old_item.item_id == self.new_item.item_id:
            return self.new_item.quantity - self.old_item.quantity
        return -self.old_item.quantity if self.new_item.empty else self.new_item.quantity


@dataclass(frozen=True, slots=True)
class SkillChanged(GameEvent):
    """A skill's xp, level or boosted level changed between two server ticks. Derived from the
    snapshot (``seq`` -1). The Java host's ``SkillChangeEvent``."""
    skill_id: int
    old_experience: int
    new_experience: int
    old_level: int
    new_level: int
    old_boosted: int
    new_boosted: int
    server_tick: int

    @property
    def experience_gained(self) -> int:
        return self.new_experience - self.old_experience

    @property
    def levels_gained(self) -> int:
        return self.new_level - self.old_level

    @property
    def is_level_up(self) -> bool:
        return self.new_level > self.old_level


@dataclass(frozen=True, slots=True)
class SpotAnim(GameEvent):
    target_server_index: int
    target_type: int
    spot_anim_id: int
    tile_x: int
    tile_y: int
    plane: int


class UiHook:
    """The ``hook`` values a :class:`UiEvent` carries. A hook the agent adds later arrives as its
    own string, through the same event type: compare ``event.hook`` with a string, never assume
    this list is complete.

    ``destroyed`` and ``overflow`` are always delivered. The others (plugin UI P2, NXTLibrary
    PUBLIC_PROTOCOL 6.12) arrive only for a hook the script subscribed on that component
    (``component.on(hook, handler)`` or ``component.subscribe_hook(hook)``), each as its own
    typed subclass of :class:`UiEvent`."""
    DESTROYED = "destroyed"   # the component is gone; ``reason`` says why
    OVERFLOW = "overflow"     # synthetic: ``dropped`` ui events were lost; ``handle`` is 0
    ONCLICK = "onclick"                         # all types: UiClick
    ONRELEASE = "onrelease"                     # all types: UiRelease
    ONMOUSEOVER = "onmouseover"                 # all types: UiMouseOver
    ONMOUSELEAVE = "onmouseleave"               # all types: UiMouseLeave
    ONBUTTONPRESSED = "onbuttonpressed"         # button, check: UiButtonPressed
    ONCONTENTCHANGED = "oncontentchanged"       # input_field: UiContentChanged
    ONSELECTIONCHANGED = "onselectionchanged"   # slider: UiSliderChanged; combo_field and
    #                                             list: UiEntrySelectionChanged

    # The hooks ``ui.subscribe`` takes (``destroyed`` needs no subscribe).
    SUBSCRIBABLE = frozenset((ONCLICK, ONRELEASE, ONMOUSEOVER, ONMOUSELEAVE, ONBUTTONPRESSED,
                              ONCONTENTCHANGED, ONSELECTIONCHANGED))


class UiContentReason:
    """``UiContentChanged.reason``: the native host's name for the client's reason code
    (``reason_code`` 0..4). ``""`` for a code the host does not know; ``reason_code`` still has
    it. A script's own content set reports ``content_change`` too."""
    CONTENT_CHANGE = "content_change"               # 0
    ERROR = "error"                                 # 1
    SUBMIT = "submit"                               # 2
    SUBMIT_ON_ESCAPE = "submit_on_escape"           # 3
    SUBMIT_ON_FOCUS_LOSS = "submit_on_focus_loss"   # 4


class UiSelectionEvent:
    """``UiEntrySelectionChanged.selection_event``: the client's code for what happened to the
    entry. The native host also names it in ``reason`` (``"deselected"``, ``"selected"``,
    ``"error_max_selected"``, ``"error_not_selectable"``, or ``""`` for a code it does not
    know)."""
    DESELECTED = 0
    SELECTED = 1
    ERROR_MAX_SELECTED = 2
    ERROR_NOT_SELECTABLE = 3


class UiDestroyReason:
    """``UiEvent.reason`` for a ``destroyed`` event (NXTLibrary PUBLIC_PROTOCOL 7.3). Strings,
    so a reason the agent adds later still arrives as itself."""
    PARENT_GONE = "parent_gone"
    REMOVED = "removed"
    SERVER_EVENTS = "server_events"
    INTERFACE_UNREADABLE = "interface_unreadable"


_NO_FIELDS: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class UiEvent:
    """One plugin-UI event (the agent's ``ui.event`` push) about a component **this script**
    made. Subscribe with ``ctx.events.subscribe(UiEvent, handler)``, or filter by handle and
    hook with ``ctx.ui.subscribe(handler, handle=..., hook=...)``.

    It is not a ring event, so it has no ``seq``. ``handle`` is the component's
    (``component.handle``); ``hook`` is a :class:`UiHook` value or a later hook's name;
    ``reason`` is the agent's reason (``""`` when it sent none), see :class:`UiDestroyReason`.
    ``fields`` holds any other key of the push (a value the host could not represent is None),
    so a later hook's payload needs no change here. ``truncated``: the host cut ``hook`` or
    ``reason`` to its field.

    The synthetic ``overflow`` event (``handle == 0``) says events were lost: ``dropped`` of
    them since the last such report. It reaches every script on the client that uses the UI,
    because a lost event's owner is unknown."""
    handle: int
    hook: str
    reason: str = ""
    truncated: bool = False
    fields: Mapping[str, Any] = field(default_factory=lambda: _NO_FIELDS)

    @property
    def is_destroyed(self) -> bool:
        return self.hook == UiHook.DESTROYED

    @property
    def is_overflow(self) -> bool:
        return self.hook == UiHook.OVERFLOW

    @property
    def dropped(self) -> int:
        """For an ``overflow`` event, how many events were lost; 0 otherwise."""
        value = self.fields.get("dropped")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @classmethod
    def from_native(cls, raw: Mapping[str, Any]) -> "UiEvent":
        """From one dict of ``botwithus._native.ui_poll_events``: the typed subclass for a
        subscribed hook (:class:`UiClick`, :class:`UiButtonPressed`, ...), else a plain
        :class:`UiEvent` (``destroyed``, ``overflow``, a hook added later).

        A known hook whose named fields are missing or of the wrong type (a host that could not
        decode the agent's args sends ``arg0``, ``arg1``, ...) is not dropped: it arrives as a
        plain :class:`UiEvent` with every field in ``fields``, and a warning is logged."""
        known = ("handle", "hook", "reason", "truncated")
        extra = {k: v for k, v in raw.items() if k not in known}
        base = {"handle": int(raw.get("handle", 0)), "hook": str(raw.get("hook", "")),
                "reason": str(raw.get("reason") or ""), "truncated": bool(raw.get("truncated")),
                "fields": MappingProxyType(extra) if extra else _NO_FIELDS}
        decode = _UI_DECODERS.get(base["hook"])
        if decode is not None:
            try:
                return decode(base, extra)
            except (KeyError, TypeError) as exc:
                _log.warning("ui: %s event for handle %s has no typed fields (%s); delivered "
                             "as a plain UiEvent", base["hook"], base["handle"], exc)
        return UiEvent(**base)


def _ui_int(fields: Mapping[str, Any], key: str) -> int:
    value = fields[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} is {type(value).__name__}, not int")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class UiMouseEvent(UiEvent):
    """A mouse hook on a component (any type). ``x``, ``y``: component relative."""
    x: int
    y: int


@dataclass(frozen=True, slots=True, kw_only=True)
class UiClick(UiMouseEvent):
    """``onclick``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UiRelease(UiMouseEvent):
    """``onrelease``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UiMouseOver(UiMouseEvent):
    """``onmouseover``. The client raises it only while its mouseover toggle is on (the
    default)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UiMouseLeave(UiMouseEvent):
    """``onmouseleave``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UiButtonPressed(UiEvent):
    """``onbuttonpressed`` on a button or a check. For a check, ``current`` is the new checked
    state."""
    current: bool
    previous: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class UiContentChanged(UiEvent):
    """``oncontentchanged`` on an input_field. ``reason`` is a :class:`UiContentReason` name,
    ``reason_code`` the client's number. ``content`` is the field's text (at most 767 bytes),
    None on a restricted field. **A script's own content set raises this too**: only context
    tells it from typed input."""
    reason_code: int
    content: str | None

    @property
    def is_submit(self) -> bool:
        """The user submitted the field (enter, escape or focus loss)."""
        return self.reason in (UiContentReason.SUBMIT, UiContentReason.SUBMIT_ON_ESCAPE,
                               UiContentReason.SUBMIT_ON_FOCUS_LOSS)


@dataclass(frozen=True, slots=True, kw_only=True)
class UiSelectionChanged(UiEvent):
    """``onselectionchanged``: the base of :class:`UiSliderChanged` (slider) and
    :class:`UiEntrySelectionChanged` (combo_field, list); subscribe to it for both. Only the
    user's input raises it: a script's own value or selection set never does."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UiSliderChanged(UiSelectionChanged):
    """``onselectionchanged`` on a slider: its new ``value``."""
    value: int


@dataclass(frozen=True, slots=True, kw_only=True)
class UiEntrySelectionChanged(UiSelectionChanged):
    """``onselectionchanged`` on a combo_field or list: ``entry_id`` and what happened to it,
    ``selection_event`` (a :class:`UiSelectionEvent` code; ``reason`` names it)."""
    entry_id: int
    selection_event: int

    @property
    def selected(self) -> bool:
        return self.selection_event == UiSelectionEvent.SELECTED


_UiDecoder = Callable[[dict[str, Any], Mapping[str, Any]], UiEvent]


def _ui_mouse(kind: type[UiMouseEvent]) -> _UiDecoder:
    return lambda base, f: kind(**base, x=_ui_int(f, "x"), y=_ui_int(f, "y"))


def _ui_pressed(base: dict[str, Any], f: Mapping[str, Any]) -> UiEvent:
    return UiButtonPressed(**base, current=bool(_ui_int(f, "current")),
                           previous=bool(_ui_int(f, "previous")))


def _ui_content(base: dict[str, Any], f: Mapping[str, Any]) -> UiEvent:
    content = f["content"]
    if content is not None and not isinstance(content, str):
        raise TypeError(f"content is {type(content).__name__}, not str")
    return UiContentChanged(**base, reason_code=_ui_int(f, "reason_code"), content=content)


def _ui_selection(base: dict[str, Any], f: Mapping[str, Any]) -> UiEvent:
    if "value" in f:
        return UiSliderChanged(**base, value=_ui_int(f, "value"))
    return UiEntrySelectionChanged(**base, entry_id=_ui_int(f, "entry_id"),
                                   selection_event=_ui_int(f, "selection_event"))


# hook -> decoder of the native host's named fields (bwu_host_surface.h, plugin UI P2).
_UI_DECODERS: dict[str, _UiDecoder] = {
    UiHook.ONCLICK: _ui_mouse(UiClick),
    UiHook.ONRELEASE: _ui_mouse(UiRelease),
    UiHook.ONMOUSEOVER: _ui_mouse(UiMouseOver),
    UiHook.ONMOUSELEAVE: _ui_mouse(UiMouseLeave),
    UiHook.ONBUTTONPRESSED: _ui_pressed,
    UiHook.ONCONTENTCHANGED: _ui_content,
    UiHook.ONSELECTIONCHANGED: _ui_selection,
}


E = TypeVar("E", bound="GameEvent | UiEvent")


class EventBus:
    """Class-keyed synchronous pub/sub for decoded game events."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type``; returns an unsubscribe callable."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

        def _off() -> None:
            with self._lock:
                lst = self._handlers.get(event_type)
                if lst and handler in lst:
                    lst.remove(handler)

        return _off

    def publish(self, event: GameEvent | UiEvent) -> None:
        with self._lock:
            handlers = list(self._handlers.get(type(event), ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                pass


# Event types that belong to one script, not to the whole client: delivered only on the bus of
# the script they concern.
SCRIPT_SCOPED_EVENTS: tuple[type, ...] = (UiEvent,)


class ScriptEventBus(EventBus):
    """One script's view of its client's :class:`EventBus` (``ctx.events``).

    Game events are the client's: subscribing to one subscribes on the shared bus, as before.
    A script-scoped event (:data:`SCRIPT_SCOPED_EVENTS`: :class:`UiEvent`) is this script's
    own, kept here: several scripts on one client never see each other's UI events."""

    def __init__(self, shared: EventBus) -> None:
        super().__init__()
        self._shared = shared

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        if issubclass(event_type, SCRIPT_SCOPED_EVENTS):
            return super().subscribe(event_type, handler)
        return self._shared.subscribe(event_type, handler)

    def publish(self, event: GameEvent | UiEvent) -> None:
        """A script-scoped event goes to the handlers of its class **and of every base class**,
        most specific first: ``subscribe(UiEvent, h)`` gets every ui event (the typed ones
        included), ``subscribe(UiSelectionChanged, h)`` slider and entry changes both, and
        ``subscribe(UiClick, h)`` only clicks."""
        if not isinstance(event, SCRIPT_SCOPED_EVENTS):
            self._shared.publish(event)
            return
        with self._lock:
            handlers = [h for kind in type(event).__mro__ for h in self._handlers.get(kind, ())]
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                _log.exception("ui event handler on ctx.events failed")
