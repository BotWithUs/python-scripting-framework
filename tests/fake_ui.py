"""A fake of the native host's plugin-UI surface (``botwithus._native.ui_*``), playing both
halves: the host's own refusals and tracking, and the agent's ``ui.*`` verbs
(``PUBLIC_PROTOCOL.md`` 6.12 / 7.3, and the agent's observed behaviour where noted).

Ownership is per attach, as it is per pipe connection on the agent: every script on one
``Connection`` shares one fake host state, exactly the case the framework's per-script tracking
exists for. ``calls`` records every ui call as ``(verb, args...)`` so a test can assert what was,
and was not, sent (no ``destroy_all`` from the framework, one ``destroy`` per component).

P2 (NXTLibrary a17edc4, native-scripting-host 7bb38a4): the interactive types, their props and
readback, ``ui_subscribe`` / ``ui_unsubscribe`` / ``ui_blur``, and hook events with the native
host's named fields. :class:`FakeUiNativeP1` is a host from before P2 (no ``ui_subscribe``).
"""
from __future__ import annotations

import threading
import time

from fakes import FakeRunlog

# The agent's and host's refusal texts, verbatim, as a script receives them.
UNKNOWN_HANDLE = "ui: unknown handle (destroyed, invalidated or not yours)"
FORBIDDEN = "ui: prop forbidden on agent components (no op, drag or target configuration)"
BAD_CATEGORY = "ui: bad_param category (1..234)"
READ_ONLY = "ui: handle is a Jagex component: read-only, usable only as a parent or with ui.get"
CLIENT_EXITED = "client exited"
TYPE_IDS = {"line": 0, "rectangle": 1, "text": 2, "canvas": 8, "layer": 20,
            "button": 30, "check": 31, "list": 32, "input_field": 33, "slider": 34,
            "combo_field": 35, "panel": 36, "grid": 37}
P2_TYPES = ("button", "check", "list", "input_field", "slider", "combo_field", "panel", "grid")
LATER_TYPES = ("sprite", "model")
MOUSE_HOOKS = ("onclick", "onrelease", "onmouseover", "onmouseleave")
HOOK_TYPES = {"onbuttonpressed": ("button", "check"), "oncontentchanged": ("input_field",),
              "onselectionchanged": ("slider", "combo_field", "list")}
HOOK_NOT_SUPPORTED = "ui: hook not supported (onclick, onrelease, onmouseover, onmouseleave, " \
                     "onbuttonpressed, oncontentchanged, onselectionchanged)"
HOOK_NOT_VALID = "ui: hook not valid for this type"
BLUR_NOT_INPUT = "ui: blur applies to input_field components only"
# Props per type (P2) and the agent's not-supported-yet refusals (UiWidgetParse.cpp a17edc4).
TYPE_PROPS = {"label": ("button",), "can_toggle": ("button",), "toggled": ("button",),
              "checked": ("check",), "content": ("input_field",), "filter": ("input_field",),
              "max_length": ("input_field",), "min": ("slider",), "max": ("slider",),
              "step": ("slider",), "ticks": ("slider",), "value": ("slider",),
              "entries": ("combo_field", "list"), "selected": ("combo_field", "list"),
              "selection_limit": ("list",), "vertical": ("panel",)}
NOT_YET = {("check", "label"): "ui prop not supported yet: label (check text is not mapped)",
           ("panel", "spacing"): "ui prop not supported yet: spacing (panel spacing is not mapped)",
           ("grid", "layout"): "ui prop not supported yet: layout (grid layout fields are not pinned)",
           ("input_field", "vis_mode"):
               "ui prop not supported yet: vis_mode (a non-zero mode marks the field restricted)"}
SLIDER_RANGE = ("min", "max", "step", "ticks")
SLIDER_TOGETHER = "ui: min, max, step and ticks go together"
FORBIDDEN_PROPS = ("op_config", "opConfig", "ops", "drag_config", "dragConfig", "target",
                   "target_config", "swipe_config", "pinch_config", "cursor_config",
                   "params_config", "flag_restricted")
FIRST_HANDLE = 0x1000
DEFAULT_CATEGORY = 1
CANVAS_OPS = ("line", "outline", "rect", "gradient_h", "gradient_v", "triangle", "clear", "undo")


class FakeUiRefused(RuntimeError):
    """``botwithus._native.UiRefused``: a RuntimeError subclass."""


class _UiState:
    def __init__(self) -> None:
        self.next_handle = FIRST_HANDLE
        self.next_dynamic = 0
        self.components: dict[int, dict] = {}   # handle -> info + props, agent side
        self.tracked: set[int] = set()          # the host's tracker
        self.events: list[dict] = []            # kept pushes, oldest first
        self.dropped = 0                        # pushes the host's cap lost, not yet reported
        self.subscribed = False
        self.deferred: list[tuple[int, str]] = []   # defer_content: sets the client has not run


def _content_event(handle: int, content: str) -> dict:
    return {"handle": handle, "hook": "oncontentchanged", "reason": "content_change",
            "truncated": False, "reason_code": 0, "content": content}


def _subscribable(hook: str) -> bool:
    return hook in MOUSE_HOOKS or hook in HOOK_TYPES


def _check_props(type_name: str, props: dict, current: dict) -> None:
    """The agent's prop checks (enough of them): all before anything is applied."""
    for prop in props:
        if (type_name, prop) in NOT_YET:
            raise FakeUiRefused(NOT_YET[(type_name, prop)])
        owners = TYPE_PROPS.get(prop)
        if owners is not None and type_name not in owners:
            raise FakeUiRefused(f"ui: {prop} applies to {' / '.join(owners)} components only")
    given = [k for k in SLIDER_RANGE if k in props]
    if given and len(given) != len(SLIDER_RANGE):
        raise FakeUiRefused(SLIDER_TOGETHER)
    if "toggled" in props and not props.get("can_toggle", current.get("can_toggle", False)):
        raise FakeUiRefused("ui: toggled needs a button that can toggle")
    for entry in props.get("entries") or ():
        if not (isinstance(entry, dict) and set(entry) == {"id", "text"}):
            raise TypeError("ui: each entry is {'id': int, 'text': str} or (id, text)")
    if type_name == "list" and "selected" in props and not isinstance(props["selected"], list):
        raise TypeError("ui: a list's selected is a list of ids")


def _apply(info: dict, props: dict) -> None:
    p = info["props"]
    p.update(props)
    if info["type_name"] == "slider" and "min" in props and "value" not in props:
        p["value"] = props["min"]   # the value starts at min unless set in the same call


def _widget(info: dict, with_content: bool) -> dict:
    """The P2 readback keys: only the type's own (no -1 placeholders)."""
    t, p = info["type_name"], info["props"]
    out: dict = {"enabled": bool(p.get("enabled", True))}
    if t == "button":
        out.update(label=p.get("label", ""), label_truncated=False,
                   can_toggle=bool(p.get("can_toggle", False)), toggled=bool(p.get("toggled", False)))
    elif t == "check":
        out["checked"] = bool(p.get("checked", False))
    elif t == "input_field":
        out.update(focused=bool(info.get("focused", False)), filter=p.get("filter", "none"),
                   max_length=p.get("max_length", 80), font_id=-1, sprite_id=-1, renders=False)
        if with_content:
            out.update(content=p.get("content", ""), content_truncated=False)
    elif t == "slider":
        out.update({k: p.get(k, 0) for k in (*SLIDER_RANGE, "value")})
    elif t in ("combo_field", "list"):
        sel = p.get("selected", [] if t == "list" else -1)
        rows = []
        for e in p.get("entries", []):
            row = {"id": e["id"], "text": e["text"], "truncated": False}
            if t == "list":
                row.update(hidden=False, disabled=False, selected=e["id"] in sel)
            rows.append(row)
        out["entries"] = rows
        out["selected"] = list(sel) if t == "list" else sel
        if t == "list":
            out["selection_limit"] = p.get("selection_limit", 1)
    elif t == "panel":
        out["vertical"] = bool(p.get("vertical", False))
    return out


class FakeUiNative(FakeRunlog):
    UiRefused = FakeUiRefused
    UI_KEY_STATIC = -1
    UI_DEFAULT = -1
    UI_CANVAS_OPS_MAX = 128
    UI_LIST_MAX = 256

    def __init__(self, *, agent_p2: bool = True, **kw) -> None:
        super().__init__(**kw)
        self.ui_lock = threading.Lock()
        self.calls: list[tuple] = []
        self.fail_next: dict[str, BaseException] = {}   # verb -> exception to raise once
        self.client_gone = False
        self.polls = 0
        self.hosts: list = []
        # block_verb: that verb, once, holds ui_lock (the host's I/O lock: ui_poll_events takes it
        # too) until `unblock` is set; `blocked` is set while it waits.
        self.block_verb: str | None = None
        self.blocked = threading.Event()
        self.unblock = threading.Event()
        # race_create: each create pushes destroyed/<reason> for its own new handle, then waits
        # race_delay_s after releasing ui_lock before it returns, so a poll can route the event
        # before the script holds the handle (the agent can invalidate a component that fast).
        self.race_create: str | None = None
        self.race_delay_s = 0.2
        self.agent_p2 = agent_p2   # False: an agent before P2 (refuses the interactive types)
        # defer_content: the client raises oncontentchanged on its game thread AFTER ui.set has
        # returned (seen live on 951-4), so a content set is held until client_tick(); only then
        # is the subscription checked, and the push reaches the next poll.
        self.defer_content = False

    def attach(self, pid):
        handle = super().attach(pid)
        handle.ui = _UiState()
        self.hosts.append(handle)
        return handle

    def detach(self, handle):
        """bwu_detach: the host's own release, one ui.destroy_all on this connection, if it
        ever sent a ui verb, as the native host does."""
        with self.ui_lock:
            st = handle.ui
            if st.subscribed:
                self.calls.append(("host_release",
                                   sum(1 for i in st.components.values() if not i["jagex"])))
                st.components.clear()
                st.tracked.clear()
        super().detach(handle)

    # --- helpers a test drives ---------------------------------------------------------------

    def client_tick(self, handle_state) -> None:
        """defer_content: the game thread runs the held content sets now. Each is pushed if
        its field is still alive and subscribed to oncontentchanged at this point."""
        with self.ui_lock:
            st = handle_state.ui
            for handle, content in st.deferred:
                info = st.components.get(handle)
                if info is not None and "oncontentchanged" in info["hooks"]:
                    st.events.append(_content_event(handle, content))
            st.deferred = []

    def focus(self, handle_state, handle: int) -> None:
        """A user's click focusing an input_field (the agent never focuses one itself)."""
        with self.ui_lock:
            handle_state.ui.components[handle]["focused"] = True

    def hooks_on(self, handle_state, handle: int) -> set[str]:
        """The hooks the agent holds for ``handle`` (empty once it is gone)."""
        with self.ui_lock:
            info = handle_state.ui.components.get(handle)
            return set(info.get("hooks", ())) if info else set()

    def push(self, handle_state, handle: int, hook: str = "destroyed", reason: str = "",
             **fields) -> None:
        """The agent pushing a ui.event for ``handle`` (kept by the host until polled). A
        subscribable hook not subscribed on ``handle`` is not kept, as the native host does
        (and an unsubscribe drops the kept ones); any other hook name passes (a later hook)."""
        with self.ui_lock:
            info = handle_state.ui.components.get(handle)
            if _subscribable(hook) and (info is None or hook not in info["hooks"]):
                return
            if hook == "destroyed":
                handle_state.ui.components.pop(handle, None)
            handle_state.ui.events.append({"handle": handle, "hook": hook, "reason": reason,
                                           "truncated": False, **fields})

    def drop_pushes(self, handle_state, n: int) -> None:
        with self.ui_lock:
            handle_state.ui.dropped += n

    def agent_components(self, handle_state) -> dict[int, dict]:
        with self.ui_lock:
            return dict(handle_state.ui.components)

    def verbs(self, verb: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == verb]

    # --- the surface ---------------------------------------------------------------------------

    def _enter(self, verb: str, *args) -> None:
        self.calls.append((verb, *args))
        exc = self.fail_next.pop(verb, None)
        if exc is not None:
            raise exc
        if self.client_gone:
            raise RuntimeError(CLIENT_EXITED)

    def _maybe_block(self, verb: str) -> None:
        """Caller holds ui_lock."""
        if self.block_verb == verb:
            self.block_verb = None
            self.blocked.set()
            self.unblock.wait()

    def _own(self, st: _UiState, handle: int) -> dict:
        info = st.components.get(handle)
        if info is None:
            st.tracked.discard(handle)   # the agent answered: the host forgets it
            raise FakeUiRefused(UNKNOWN_HANDLE)
        return info

    def _edit(self, st: _UiState, handle: int) -> dict:
        info = st.components.get(handle)
        if info is not None and info["jagex"]:
            raise ValueError(READ_ONLY)
        return self._own(st, handle)

    @staticmethod
    def _public(info: dict) -> dict:
        return {k: info[k] for k in ("handle", "parent", "type", "type_name", "iface", "comp",
                                     "key", "jagex", "quarantined", "truncated")}

    def ui_get_component(self, h, iface, comp, key=-1):
        self._enter("get_component", iface, comp, key)
        with self.ui_lock:
            st = h.ui
            st.subscribed = True
            for info in st.components.values():   # observed: the agent dedups lookups per connection
                if info["jagex"] and (info["iface"], info["comp"], info["key"]) == (iface, comp, key):
                    return self._public(info)
            handle = st.next_handle
            st.next_handle += 1
            info = {"handle": handle, "parent": 0, "type": 20, "type_name": "layer",
                    "iface": iface, "comp": comp, "key": key, "jagex": True,
                    "quarantined": False, "truncated": False, "props": {}, "children": []}
            st.components[handle] = info
            st.tracked.add(handle)
            return self._public(info)

    def ui_create(self, h, parent, type, props=None, category=None, dynamic=None):
        self._enter("create", parent, type, props, category, dynamic)
        props = dict(props or {})
        if any(p in FORBIDDEN_PROPS for p in props):
            raise ValueError(FORBIDDEN)
        if category == 0:
            raise ValueError(BAD_CATEGORY)
        with self.ui_lock:
            self._maybe_block("create")
            st = h.ui
            st.subscribed = True
            parent_info = self._own(st, parent)
            if type in LATER_TYPES or (type in P2_TYPES and not self.agent_p2):
                raise FakeUiRefused(f"ui type not supported yet: {type}")
            if type not in TYPE_IDS:
                raise FakeUiRefused("ui: unknown type")
            _check_props(type, props, {})
            handle = st.next_handle
            st.next_handle += 1
            key = (category or DEFAULT_CATEGORY) << 16 | (st.next_dynamic if dynamic is None else dynamic)
            st.next_dynamic += 1
            info = {"handle": handle, "parent": parent, "type": TYPE_IDS[type], "type_name": type,
                    "iface": parent_info["iface"], "comp": parent_info["comp"], "key": key,
                    "jagex": False, "quarantined": False, "truncated": False, "props": {},
                    "children": [], "elements": 0 if type == "canvas" else -1, "hooks": set()}
            _apply(info, props)
            st.components[handle] = info
            parent_info["children"].append(handle)
            st.tracked.add(handle)
            out = self._public(info)
            if self.race_create:
                st.components.pop(handle, None)
                st.events.append({"handle": handle, "hook": "destroyed",
                                  "reason": self.race_create, "truncated": False})
        if self.race_create:
            time.sleep(self.race_delay_s)
        return out

    def ui_set(self, h, handle, props):
        self._enter("set", handle, props)
        if any(p in FORBIDDEN_PROPS for p in props):
            raise ValueError(FORBIDDEN)
        with self.ui_lock:
            info = self._edit(h.ui, handle)
            _check_props(info["type_name"], props, info["props"])
            _apply(info, props)
            # Only a content set raises the client's change hook; a value or selection never.
            if "content" in props and self.defer_content:
                h.ui.deferred.append((handle, props["content"]))
            elif "content" in props and "oncontentchanged" in info["hooks"]:
                h.ui.events.append(_content_event(handle, props["content"]))

    def ui_get(self, h, handle, with_content=False):
        self._enter("get", handle, with_content)
        with self.ui_lock:
            info = self._own(h.ui, handle)
            p = info["props"]
            out = self._public(info)
            out.update({"x": p.get("x", 0), "y": p.get("y", 0), "width": p.get("width", 0),
                        "height": p.get("height", 0), "rgba": p.get("rgba", 0), "visible": True,
                        "restricted": False, "clickthrough": bool(p.get("clickthrough", False)),
                        "num_elements": info.get("elements", -1), "line_width": -1,
                        "line_invert": -1, "fill": -1, "outline_thickness": -1, "use_count": 2,
                        "op_flags_read": True, "op_mask": 0, "op_target": -1})
            if with_content and info["type_name"] == "text":
                out["content"] = p.get("text", "")
                out["content_truncated"] = False
            if not info["jagex"] and self.agent_p2 and self.ui_subscribe is not None:
                out.update(_widget(info, with_content))
            return out

    def ui_subscribe(self, h, handle, hook):
        self._enter("subscribe", handle, hook)
        with self.ui_lock:
            info = self._edit(h.ui, handle)   # a Jagex handle: refused without sending
            if hook not in MOUSE_HOOKS and hook not in HOOK_TYPES:
                raise FakeUiRefused(HOOK_NOT_SUPPORTED)
            if hook in HOOK_TYPES and info["type_name"] not in HOOK_TYPES[hook]:
                raise FakeUiRefused(HOOK_NOT_VALID)
            info["hooks"].add(hook)

    def ui_unsubscribe(self, h, handle, hook):
        self._enter("unsubscribe", handle, hook)
        with self.ui_lock:
            st = h.ui
            if handle not in st.tracked:
                return False   # the host does not send: the agent dropped them with the handle
            info = st.components.get(handle)
            if info is None:
                st.tracked.discard(handle)
                return False   # the agent's "unknown handle" is a success with False
            was = hook in info["hooks"]
            info["hooks"].discard(hook)
            # Nothing arrives after unsubscribe returns: the kept pushes go too.
            st.events = [e for e in st.events if (e["handle"], e["hook"]) != (handle, hook)]
            return was

    def ui_blur(self, h, handle):
        self._enter("blur", handle)
        with self.ui_lock:
            info = self._edit(h.ui, handle)
            if info["type_name"] != "input_field":
                raise FakeUiRefused(BLUR_NOT_INPUT)
            had = bool(info.get("focused", False))
            info["focused"] = False
            return had

    def ui_canvas(self, h, handle, ops):
        self._enter("canvas", handle, [dict(o) for o in ops])
        if not 1 <= len(ops) <= self.UI_CANVAS_OPS_MAX:
            raise ValueError("ui_canvas: 1..128 ops")
        with self.ui_lock:
            info = self._edit(h.ui, handle)
            if any(op["op"] not in CANVAS_OPS for op in ops):
                raise FakeUiRefused("ui.canvas: unknown op")   # all or nothing, as the agent
            for op in ops:
                if op["op"] == "clear":
                    info["elements"] = 0
                elif op["op"] == "undo":
                    info["elements"] = max(0, info["elements"] - 1)
                else:
                    info["elements"] += 1
            return info["elements"]

    def ui_order(self, h, handle, to):
        self._enter("order", handle, to)
        with self.ui_lock:
            self._edit(h.ui, handle)
            if to not in ("front", "back"):
                raise FakeUiRefused("ui: bad_param to (front | back)")

    def _remove_tree(self, st: _UiState, handle: int) -> None:
        info = st.components.pop(handle, None)
        if info is None:
            return
        for child in info["children"]:
            self._remove_tree(st, child)

    def ui_destroy(self, h, handle):
        self._enter("destroy", handle)
        with self.ui_lock:
            st = h.ui
            self._edit(st, handle)
            self._remove_tree(st, handle)   # children go with it, with no event (7.3)
            st.tracked.discard(handle)
            return True

    def ui_destroy_all(self, h):
        self._enter("destroy_all")
        with self.ui_lock:
            st = h.ui
            # Observed agent behaviour: destroy_all's count excludes Jagex lookups.
            n = sum(1 for info in st.components.values() if not info["jagex"])
            st.components.clear()
            st.tracked.clear()
            return n

    def ui_list(self, h):
        self._enter("list")
        with self.ui_lock:
            return [self._public(i) for i in h.ui.components.values()]

    def ui_poll_events(self, h):
        # Not recorded in calls: the pump polls every 20 ms.
        self.polls += 1
        if self.client_gone:
            raise RuntimeError(CLIENT_EXITED)
        with self.ui_lock:
            st = h.ui
            if not st.subscribed:
                return []
            out = []
            if st.dropped:
                out.append({"handle": 0, "hook": "overflow", "reason": "", "truncated": False,
                            "dropped": st.dropped})
                st.dropped = 0
            for ev in st.events:
                if ev["handle"] not in st.tracked:
                    continue   # routed by handle: only this host's handles
                if ev["hook"] == "destroyed":
                    st.tracked.discard(ev["handle"])
                out.append(ev)
            st.events = []
            return out


class FakeUiNativeP1(FakeUiNative):
    """A native host from before P2: no ``ui_subscribe`` / ``ui_unsubscribe`` / ``ui_blur``."""
    ui_subscribe = None
    ui_unsubscribe = None
    ui_blur = None

    def __init__(self, **kw) -> None:
        super().__init__(agent_p2=False, **kw)
