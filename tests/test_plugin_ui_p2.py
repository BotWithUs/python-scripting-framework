"""Plugin UI P2 (``ctx.ui``'s interactive types, hooks and blur) against the fake native host:
every helper and its props and readback, ``subscribe_hook`` / ``unsubscribe_hook`` / ``on``, the
typed event decode for every hook, refusals passed through verbatim, routing to the owning
script, and the release on stop (no ``ui.unsubscribe``, no ``ui.destroy_all``)."""
from __future__ import annotations

import threading
import time

import pytest

from botwithus.api import BotScript, script_manifest
from botwithus.api import ui as ui_mod
from botwithus.api.events import (UiButtonPressed, UiClick, UiContentChanged, UiContentReason,
                                  UiEntrySelectionChanged, UiEvent, UiHook, UiMouseEvent,
                                  UiMouseLeave, UiMouseOver, UiRelease, UiSelectionChanged,
                                  UiSelectionEvent, UiSliderChanged)
from botwithus.api.ui import (Button, Check, ComboField, ComponentType, Grid, InputField, ListBox,
                              Panel, Slider, UiEntry, UiEntryRead)
from botwithus.core import native as native_mod
from botwithus.core.connection import Connection
from botwithus.core.native import NativeUnavailable

from fake_ui import (BLUR_NOT_INPUT, HOOK_NOT_SUPPORTED, HOOK_NOT_VALID, NOT_YET, READ_ONLY,
                     SLIDER_TOGETHER, UNKNOWN_HANDLE, FakeUiNative, FakeUiNativeP1, FakeUiRefused)

GAME_AREA = (1477, 61)   # TOPLEVEL_V2__GAME_AREA
EVENT_WAIT_S = 2.0
QUIET_S = 0.15           # long enough for several 20 ms ui polls
POLL_S = 0.01
ON_STOP_S = 0.2       # OnStopRace: an on_stop that takes a while


def wait_until(pred, timeout_s: float = EVENT_WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(POLL_S)
    return pred()


@pytest.fixture
def fake():
    f = FakeUiNative()
    native_mod.set_backend(f)
    yield f
    native_mod.set_backend(None)


@pytest.fixture
def conn(fake):
    c = Connection(fake._pid, enable_navigation=False)
    yield c
    c.close()


def _ctx(conn, name="s"):
    return conn.new_context(name, threading.Event())


def _raw(handle, hook, reason="", **fields):
    return {"handle": handle, "hook": hook, "reason": reason, "truncated": False, **fields}


# --- helpers, props, readback -------------------------------------------------------------------

def test_every_p2_helper_sends_its_type_and_returns_its_wrapper(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    panel = ui.panel(root, vertical=True, width=220)
    made = [
        ui.button(panel, "Go", can_toggle=True),
        ui.check(panel, checked=True),
        ui.input_field(panel, "hi", max_length=20),
        ui.slider(panel, min=0, max=100, step=10, ticks=10, value=20),
        ui.combo_field(panel, [UiEntry(1, "a")], selected=1),
        ui.list(panel, [(1, "Oak"), {"id": 2, "text": "Yew"}], selection_limit=3),
        ui.grid(panel),
    ]
    assert [type(c) for c in [panel, *made]] == [Panel, Button, Check, InputField, Slider,
                                                ComboField, ListBox, Grid]
    sent = [(c[2], c[3]) for c in fake.verbs("create")]
    assert sent == [
        ("panel", {"vertical": True, "width": 220}),
        ("button", {"label": "Go", "can_toggle": True}),
        ("check", {"checked": True}),
        ("input_field", {"content": "hi", "max_length": 20}),
        ("slider", {"min": 0, "max": 100, "step": 10, "ticks": 10, "value": 20}),
        ("combo_field", {"entries": [{"id": 1, "text": "a"}], "selected": 1}),
        ("list", {"entries": [{"id": 1, "text": "Oak"}, {"id": 2, "text": "Yew"}],
                  "selection_limit": 3}),
        ("grid", None),
    ]
    assert ui.owned() == [panel.handle, *(c.handle for c in made)]


def test_create_by_type_name_maps_every_p2_type_to_its_wrapper(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    wrappers = {ComponentType.BUTTON: Button, ComponentType.CHECK: Check,
                ComponentType.INPUT_FIELD: InputField, ComponentType.COMBO_FIELD: ComboField,
                ComponentType.LIST: ListBox, ComponentType.PANEL: Panel, ComponentType.GRID: Grid}
    for kind, wrapper in wrappers.items():
        assert type(ui.create(root, kind)) is wrapper
    assert type(ui.create(root, "slider", min=0, max=10, step=1, ticks=10)) is Slider


def test_button_and_check_props_and_readback(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    button = ui.button(root, "one", can_toggle=True)
    button.set_label("two")
    button.set_toggled(True)
    back = button.get()
    assert (back.label, back.label_truncated, back.can_toggle, back.toggled, back.enabled) == \
        ("two", False, True, True, True)
    assert back.checked is None and back.value is None and back.entries is None
    check = ui.check(root)
    assert check.is_checked() is False
    check.set_checked(True)
    assert check.is_checked() is True and check.get().label is None
    check.set_enabled(False)
    assert check.get().enabled is False
    assert fake.verbs("set")[-1] == ("set", check.handle, {"enabled": False})


def test_input_field_props_readback_and_renders_reported_as_is(conn, fake):
    ui = _ctx(conn).ui
    field = ui.input_field(ui.component(*GAME_AREA), filter="numeric", max_length=5)
    field.set_content("123")
    back = field.get(content=True)
    assert (back.content, back.filter, back.max_length, back.focused) == ("123", "numeric", 5, False)
    assert (back.renders, back.font_id, back.sprite_id) == (False, -1, -1)   # not hidden
    assert field.get().content is None
    assert field.read_content() == "123"


def test_slider_range_value_and_readback(conn, fake):
    ui = _ctx(conn).ui
    slider = ui.slider(ui.component(*GAME_AREA), min=0, max=100, step=10, ticks=10)
    assert slider.read_value() == 0                    # starts at min
    slider.set_value(30)
    assert slider.read_value() == 30
    slider.set_range(10, 50, 5, 8, value=25)
    assert fake.verbs("set")[-1] == ("set", slider.handle,
                                     {"min": 10, "max": 50, "step": 5, "ticks": 8, "value": 25})
    back = slider.get()
    assert (back.min, back.max, back.step, back.ticks, back.value) == (10, 50, 5, 8, 25)
    slider.set_range(0, 10, 1, 10)
    assert slider.read_value() == 0                    # the client resets it without a value


def test_combo_field_entries_and_selection(conn, fake):
    ui = _ctx(conn).ui
    combo = ui.combo_field(ui.component(*GAME_AREA))
    combo.set_entries([UiEntry(5, "five"), (6, "six")])
    assert combo.read_selected() is None
    combo.select(6)
    back = combo.get()
    assert back.entries == (UiEntryRead(5, "five"), UiEntryRead(6, "six")) and back.selected == 6
    assert back.selection_limit is None
    combo.select(None)
    assert fake.verbs("set")[-1] == ("set", combo.handle, {"selected": -1})
    assert combo.read_selected() is None


def test_list_entries_selection_and_limit(conn, fake):
    ui = _ctx(conn).ui
    box = ui.list(ui.component(*GAME_AREA), (UiEntry(i, f"e{i}") for i in range(1, 4)),
                  selection_limit=3)
    box.select(i for i in (1, 3))                      # any iterable of ids
    assert fake.verbs("set")[-1] == ("set", box.handle, {"selected": [1, 3]})
    back = box.get()
    assert back.selected == (1, 3) and back.selection_limit == 3
    assert [(e.id, e.selected) for e in back.entries] == [(1, True), (2, False), (3, True)]
    assert box.read_selected() == (1, 3)
    box.select([])
    assert box.read_selected() == ()
    box.set(selected={2})                              # a set goes as a list too
    assert box.read_selected() == (2,)
    box.set_selection_limit(1)
    assert box.get().selection_limit == 1


def test_panel_and_grid_readback(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    panel = ui.panel(root)
    panel.set_vertical(True)
    assert panel.get().vertical is True
    grid = ui.grid(root).get()
    assert grid.enabled is True and grid.vertical is None and grid.label is None


def test_a_p1_readback_has_no_p2_fields_but_enabled(conn, fake):
    ui = _ctx(conn).ui
    back = ui.text(ui.component(*GAME_AREA), "x").get(content=True)
    assert back.content == "x" and back.enabled is True
    assert all(getattr(back, f) is None for f in ("label", "checked", "focused", "renders", "min",
                                                  "value", "entries", "selected", "vertical"))


# --- refusals -------------------------------------------------------------------------------------

@pytest.mark.parametrize("type_name,prop,value", [
    ("check", "label", "x"), ("panel", "spacing", 4), ("grid", "layout", 1),
    ("input_field", "vis_mode", 1)])
def test_props_not_supported_yet_are_refused_verbatim(conn, fake, type_name, prop, value):
    ui = _ctx(conn).ui
    with pytest.raises(FakeUiRefused) as err:
        ui.create(ui.component(*GAME_AREA), type_name, **{prop: value})
    assert str(err.value) == NOT_YET[(type_name, prop)]
    assert ui.owned() == []


def test_other_p2_refusals_pass_through_unwrapped(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    slider = ui.slider(root, min=0, max=10, step=1, ticks=10)
    with pytest.raises(FakeUiRefused, match=SLIDER_TOGETHER):
        slider.set(min=1)
    with pytest.raises(FakeUiRefused, match="label applies to button"):
        slider.set(label="x")
    with pytest.raises(FakeUiRefused) as err:
        slider.subscribe_hook(UiHook.ONBUTTONPRESSED)
    assert str(err.value) == HOOK_NOT_VALID
    with pytest.raises(FakeUiRefused) as err:
        slider.subscribe_hook("ondrag")
    assert str(err.value) == HOOK_NOT_SUPPORTED
    with pytest.raises(FakeUiRefused) as err:
        ui._call("blur", slider.handle)              # blur is InputField's; the agent refuses it
    assert str(err.value) == BLUR_NOT_INPUT
    assert slider.hooks() == frozenset()
    fake.fail_next["subscribe"] = TimeoutError("rpc_timeout")
    with pytest.raises(TimeoutError):
        slider.on_changed(print)
    with pytest.raises(ValueError, match="Jagex"):   # the host refuses a Jagex handle unsent
        ui._subscribe_hook(root.handle, UiHook.ONCLICK)
    assert READ_ONLY.startswith("ui: handle is a Jagex")


def test_unsubscribing_a_hook_the_agent_does_not_know_is_refused(conn, fake):
    field = _ctx(conn).ui.input_field(_ctx(conn).ui.component(*GAME_AREA))
    fake.fail_next["unsubscribe"] = FakeUiRefused(HOOK_NOT_SUPPORTED)
    with pytest.raises(FakeUiRefused):
        field.unsubscribe_hook("ondrag")


def test_an_agent_unknown_handle_on_unsubscribe_is_success(conn, fake):
    ui = _ctx(conn).ui
    field = ui.input_field(ui.component(*GAME_AREA))
    field.subscribe_hook(UiHook.ONCONTENTCHANGED)
    fake.fail_next["unsubscribe"] = FakeUiRefused(UNKNOWN_HANDLE)
    assert field.unsubscribe_hook(UiHook.ONCONTENTCHANGED) is False
    assert field.hooks() == frozenset()


def test_has_p2_is_detected_by_ui_subscribe_not_the_version(fake):
    assert native_mod.NativeHost.__dict__["has_ui_p2"]   # a property on the seam
    old = FakeUiNativeP1()
    native_mod.set_backend(old)
    try:
        c = Connection(old._pid, enable_navigation=False)
        try:
            ui = _ctx(c).ui
            assert ui.has_p2 is False
            root = ui.component(*GAME_AREA)
            with pytest.raises(FakeUiRefused, match="ui type not supported yet: button"):
                ui.button(root)                       # an agent before P2 refuses it verbatim
            layer = ui.layer(root)
            with pytest.raises(NativeUnavailable, match="ui_subscribe"):
                layer.subscribe_hook(UiHook.ONCLICK)
            assert layer.get().enabled is None       # a P1 host reads no P2 keys
        finally:
            c.close()
    finally:
        native_mod.set_backend(fake)


def test_has_p2_on_a_p2_host(conn):
    assert _ctx(conn).ui.has_p2 is True


# --- typed decode -----------------------------------------------------------------------------

@pytest.mark.parametrize("hook,kind", [
    (UiHook.ONCLICK, UiClick), (UiHook.ONRELEASE, UiRelease),
    (UiHook.ONMOUSEOVER, UiMouseOver), (UiHook.ONMOUSELEAVE, UiMouseLeave)])
def test_mouse_hooks_decode_to_their_class(hook, kind):
    ev = UiEvent.from_native(_raw(9, hook, x=12, y=-3))
    assert type(ev) is kind and isinstance(ev, UiMouseEvent) and isinstance(ev, UiEvent)
    assert (ev.handle, ev.hook, ev.x, ev.y) == (9, hook, 12, -3)
    assert ev.fields == {"x": 12, "y": -3}            # the raw fields stay readable


def test_button_pressed_decodes_to_bools():
    ev = UiEvent.from_native(_raw(9, "onbuttonpressed", current=1, previous=0))
    assert type(ev) is UiButtonPressed and (ev.current, ev.previous) == (True, False)


@pytest.mark.parametrize("reason,code,content,submit", [
    (UiContentReason.CONTENT_CHANGE, 0, "abc", False), (UiContentReason.SUBMIT, 2, "x", True),
    (UiContentReason.SUBMIT_ON_ESCAPE, 3, None, True), ("", 9, "", False)])
def test_content_changed_decodes(reason, code, content, submit):
    ev = UiEvent.from_native(_raw(9, "oncontentchanged", reason, reason_code=code,
                                  content=content))
    assert type(ev) is UiContentChanged
    assert (ev.reason, ev.reason_code, ev.content, ev.is_submit) == (reason, code, content, submit)


def test_selection_changed_decodes_per_shape():
    slider = UiEvent.from_native(_raw(9, "onselectionchanged", value=40))
    assert type(slider) is UiSliderChanged and slider.value == 40
    entry = UiEvent.from_native(_raw(9, "onselectionchanged", "selected", entry_id=3,
                                     selection_event=UiSelectionEvent.SELECTED))
    assert type(entry) is UiEntrySelectionChanged
    assert (entry.entry_id, entry.selection_event, entry.selected, entry.reason) == \
        (3, 1, True, "selected")
    off = UiEvent.from_native(_raw(9, "onselectionchanged", "deselected", entry_id=3,
                                   selection_event=0))
    assert not off.selected
    assert all(isinstance(e, UiSelectionChanged) for e in (slider, entry, off))


@pytest.mark.parametrize("raw", [
    _raw(9, "onclick", arg0=1, arg1=2),                              # the host's fallback shape
    _raw(9, "onbuttonpressed", current=True, previous=0),            # bool is not an int here
    _raw(9, "oncontentchanged", reason_code=0, content=5),
    _raw(9, "onselectionchanged", entry_id=1)])
def test_an_undecodable_known_hook_degrades_to_a_plain_ui_event(raw):
    ev = UiEvent.from_native(raw)
    assert type(ev) is UiEvent and ev.hook == raw["hook"]
    assert dict(ev.fields) == {k: v for k, v in raw.items()
                               if k not in ("handle", "hook", "reason", "truncated")}


def test_destroyed_overflow_and_unknown_hooks_stay_plain_ui_events():
    for raw in (_raw(9, "destroyed", "removed"), _raw(0, "overflow", dropped=2),
                _raw(9, "ondrag", a=1)):
        assert type(UiEvent.from_native(raw)) is UiEvent
    assert UiEvent.from_native(_raw(9, "destroyed", "removed")) == \
        UiEvent(handle=9, hook="destroyed", reason="removed")


# --- events end to end ------------------------------------------------------------------------

def test_typed_handlers_get_typed_events_through_the_router(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    button, slider = ctx.ui.button(root), ctx.ui.slider(root, min=0, max=10, step=1, ticks=10)
    box = ctx.ui.list(root, [(1, "a")])
    pressed, moved, picked, clicks = [], [], [], []
    button.on_pressed(pressed.append)
    slider.on_changed(moved.append)
    box.on_selection_changed(picked.append)
    button.on_click(clicks.append)
    assert fake.verbs("subscribe") == [
        ("subscribe", button.handle, "onbuttonpressed"),
        ("subscribe", slider.handle, "onselectionchanged"),
        ("subscribe", box.handle, "onselectionchanged"),
        ("subscribe", button.handle, "onclick")]
    host = fake.hosts[0]
    fake.push(host, button.handle, "onbuttonpressed", "", current=1, previous=0)
    fake.push(host, slider.handle, "onselectionchanged", "", value=7)
    fake.push(host, box.handle, "onselectionchanged", "selected", entry_id=1, selection_event=1)
    fake.push(host, button.handle, "onclick", "", x=4, y=5)
    assert wait_until(lambda: pressed and moved and picked and clicks)
    assert pressed == [UiButtonPressed(handle=button.handle, hook="onbuttonpressed",
                                       fields=pressed[0].fields, current=True, previous=False)]
    assert (moved[0].value, picked[0].entry_id, clicks[0].x) == (7, 1, 4)
    assert button.hooks() == frozenset({"onbuttonpressed", "onclick"})


def test_the_event_bus_dispatches_by_class_and_base_class(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    slider = ctx.ui.slider(root, min=0, max=10, step=1, ticks=10)
    box = ctx.ui.list(root, [(1, "a")])
    slider.subscribe_hook(UiHook.ONSELECTIONCHANGED)
    box.subscribe_hook(UiHook.ONSELECTIONCHANGED)
    every, selections, sliders, clicks = [], [], [], []
    ctx.events.subscribe(UiEvent, every.append)
    ctx.events.subscribe(UiSelectionChanged, selections.append)
    ctx.events.subscribe(UiSliderChanged, sliders.append)
    ctx.events.subscribe(UiClick, clicks.append)
    host = fake.hosts[0]
    fake.push(host, slider.handle, "onselectionchanged", "", value=3)
    fake.push(host, box.handle, "onselectionchanged", "selected", entry_id=1, selection_event=1)
    assert wait_until(lambda: len(every) == 2)
    fake.push(host, box.handle, "destroyed", "removed")
    assert wait_until(lambda: len(every) == 3)
    assert [type(e) for e in every] == [UiSliderChanged, UiEntrySelectionChanged, UiEvent]
    assert [type(e) for e in selections] == [UiSliderChanged, UiEntrySelectionChanged]
    assert [e.value for e in sliders] == [3] and clicks == []


def test_a_typed_helper_never_gets_an_undecodable_event(conn, fake):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    typed, raw = [], []
    button.on_pressed(typed.append)
    ctx.events.subscribe(UiEvent, raw.append)
    fake.push(fake.hosts[0], button.handle, "onbuttonpressed", "", arg0=1, arg1=0)
    assert wait_until(lambda: raw)
    time.sleep(QUIET_S)
    assert typed == [] and type(raw[0]) is UiEvent and raw[0].fields == {"arg0": 1, "arg1": 0}


def test_a_content_set_raises_content_changed_and_value_sets_raise_nothing(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    field = ctx.ui.input_field(root)
    slider = ctx.ui.slider(root, min=0, max=100, step=10, ticks=10)
    box = ctx.ui.list(root, [(1, "a"), (2, "b")], selection_limit=3)
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    field.on_content_changed(lambda e: None)
    slider.on_changed(lambda e: None)
    box.on_selection_changed(lambda e: None)
    slider.set_value(40)
    box.select([1, 2])
    field.set_content("smoke")
    assert wait_until(lambda: got)
    time.sleep(QUIET_S)
    (ev,) = got
    assert type(ev) is UiContentChanged
    assert (ev.handle, ev.reason, ev.reason_code, ev.content) == \
        (field.handle, UiContentReason.CONTENT_CHANGE, 0, "smoke")


def test_typed_events_reach_only_the_owning_script(conn, fake):
    a, b = _ctx(conn, "a"), _ctx(conn, "b")
    btn_a = a.ui.button(a.ui.component(*GAME_AREA))
    btn_b = b.ui.button(b.ui.component(*GAME_AREA))
    got_a, got_b = [], []
    btn_a.on_pressed(got_a.append)
    a.events.subscribe(UiEvent, got_a.append)
    b.events.subscribe(UiEvent, got_b.append)
    btn_b.subscribe_hook(UiHook.ONBUTTONPRESSED)
    fake.push(fake.hosts[0], btn_a.handle, "onbuttonpressed", "", current=1, previous=0)
    assert wait_until(lambda: len(got_a) == 2)
    time.sleep(QUIET_S)
    assert got_b == [] and all(type(e) is UiButtonPressed for e in got_a)


def test_blur_drops_focus_and_says_whether_it_had_it(conn, fake):
    ui = _ctx(conn).ui
    field = ui.input_field(ui.component(*GAME_AREA))
    assert field.blur() is False
    fake.focus(fake.hosts[0], field.handle)
    assert field.get().focused is True
    assert field.blur() is True
    assert field.get().focused is False
    assert fake.verbs("blur") == [("blur", field.handle), ("blur", field.handle)]


# --- subscribe / unsubscribe bookkeeping ----------------------------------------------------------

def test_subscribe_and_unsubscribe_hook_send_the_verbs_and_track_the_hook(conn, fake):
    ui = _ctx(conn).ui
    field = ui.input_field(ui.component(*GAME_AREA))
    field.subscribe_hook(UiHook.ONCONTENTCHANGED)
    field.subscribe_hook(UiHook.ONCLICK)
    host = fake.hosts[0]
    assert fake.hooks_on(host, field.handle) == {"oncontentchanged", "onclick"}
    assert field.hooks() == frozenset({"oncontentchanged", "onclick"})
    assert field.unsubscribe_hook(UiHook.ONCLICK) is True
    assert field.unsubscribe_hook(UiHook.ONCLICK) is False         # was not subscribed
    assert fake.hooks_on(host, field.handle) == {"oncontentchanged"}
    assert field.hooks() == frozenset({"oncontentchanged"})
    assert fake.verbs("unsubscribe") == [("unsubscribe", field.handle, "onclick")] * 2


def test_off_unsubscribes_only_with_the_last_handler_and_never_raises(conn, fake):
    ui = _ctx(conn).ui
    button = ui.button(ui.component(*GAME_AREA))
    off_one = button.on_pressed(lambda e: None)
    off_two = button.on(UiHook.ONBUTTONPRESSED, lambda e: None)
    off_one()
    assert fake.verbs("unsubscribe") == []                         # one handler left
    fake.fail_next["unsubscribe"] = TimeoutError("rpc_timeout")
    off_two()                                                       # logged, not raised
    assert fake.verbs("unsubscribe") == [("unsubscribe", button.handle, "onbuttonpressed")]
    off_two()                                                       # idempotent
    assert len(fake.verbs("unsubscribe")) == 1


def test_on_destroyed_needs_no_subscribe(conn, fake):
    ctx = _ctx(conn)
    layer = ctx.ui.layer(ctx.ui.component(*GAME_AREA))
    got = []
    off = layer.on(UiHook.DESTROYED, got.append)
    assert fake.verbs("subscribe") == []
    fake.push(fake.hosts[0], layer.handle, "destroyed", "parent_gone")
    assert wait_until(lambda: got)
    off()
    assert fake.verbs("unsubscribe") == []


def test_a_failed_subscribe_leaves_no_handler(conn, fake):
    ctx = _ctx(conn)
    slider = ctx.ui.slider(ctx.ui.component(*GAME_AREA), min=0, max=10, step=1, ticks=10)
    with pytest.raises(FakeUiRefused, match=HOOK_NOT_VALID):
        slider.on(UiHook.ONCONTENTCHANGED, print)
    assert ctx.ui._handlers == [] and slider.hooks() == frozenset()


def test_unsubscribe_on_a_destroyed_component_succeeds_and_sends_nothing(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    gone, dead = ctx.ui.button(root), ctx.ui.check(root)
    off = gone.on_pressed(lambda e: None)
    dead.subscribe_hook(UiHook.ONBUTTONPRESSED)
    gone.destroy()
    fake.push(fake.hosts[0], dead.handle, "destroyed", "parent_gone")
    assert wait_until(lambda: not dead.alive)
    assert gone.hooks() == frozenset() and dead.hooks() == frozenset()
    assert gone.unsubscribe_hook(UiHook.ONBUTTONPRESSED) is False
    assert dead.unsubscribe_hook(UiHook.ONBUTTONPRESSED) is False
    off()
    assert fake.verbs("unsubscribe") == []


def test_a_destroyed_event_racing_the_subscribe_reply_leaves_nothing_tracked(conn, fake):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    real = fake.ui_subscribe

    def subscribe_then_lose_it(h, handle, hook):
        real(h, handle, hook)
        ctx.ui._router.route([_raw(handle, "destroyed", "removed")])   # routed before the reply

    fake.ui_subscribe = subscribe_then_lose_it
    button.subscribe_hook(UiHook.ONCLICK)
    assert button.hooks() == frozenset() and not button.alive


# --- release on stop ------------------------------------------------------------------------------

class Panelist(BotScript):
    """Builds a panel of interactive components with hooks subscribed, then idles."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.handles: list[int] = []
        self.offs = []

    def on_start(self, ctx) -> None:
        self.ctx = ctx
        ui = ctx.ui
        panel = ui.panel(ui.component(*GAME_AREA), vertical=True)
        slider = ui.slider(panel, min=0, max=100, step=10, ticks=10)
        box = ui.list(panel, [(1, "Oak"), (2, "Yew")], selection_limit=3)
        field = ui.input_field(panel)
        self.offs = [slider.on_changed(lambda e: None), box.on_selection_changed(lambda e: None),
                     field.on_content_changed(lambda e: None)]
        self.field = field
        self.handles = [panel.handle, slider.handle, box.handle, field.handle]
        self.ready.set()

    def on_loop(self) -> int:
        return 20


@script_manifest(name="P2A")
class PanelistA(Panelist):
    pass


@script_manifest(name="P2B")
class PanelistB(Panelist):
    pass


def test_stop_destroys_the_components_and_sends_no_unsubscribe(conn, fake):
    a, b = PanelistA(), PanelistB()
    conn.start_script(a)
    conn.start_script(b)
    assert a.ready.wait(EVENT_WAIT_S) and b.ready.wait(EVENT_WAIT_S)
    host = fake.hosts[0]
    assert fake.hooks_on(host, a.handles[1]) == {"onselectionchanged"}
    conn.stop_script("P2A")
    assert [c[1] for c in fake.verbs("destroy")] == list(reversed(a.handles))
    assert fake.verbs("unsubscribe") == [] and fake.verbs("destroy_all") == []
    alive = fake.agent_components(host)
    assert all(h not in alive for h in a.handles)
    assert fake.hooks_on(host, b.handles[3]) == {"oncontentchanged"}   # B keeps its hooks
    for off in a.offs:
        off()                                         # after stop: a no-op, never raises
    assert fake.verbs("unsubscribe") == []
    assert a.field.unsubscribe_hook(UiHook.ONCONTENTCHANGED) is False   # dropped: success
    sent = len(fake.calls)
    for call in (lambda: a.field.subscribe_hook(UiHook.ONCLICK), a.field.blur,
                 lambda: a.ctx.ui.button(a.ctx.ui.component(*GAME_AREA))):
        with pytest.raises(RuntimeError, match=ui_mod.STOPPED):
            call()
    assert len(fake.calls) == sent                    # refused before anything was sent
    conn.stop_script("P2B")
    assert fake.verbs("unsubscribe") == [] and fake.verbs("destroy_all") == []


# --- delivery after stop, destroy and off() (review H1 / M1) -------------------------------------

def _route(ctx, *raws):
    """Route ``raws`` as one batch, on this thread, the way the ui-events thread does."""
    return ctx.ui._router.route(list(raws))


def test_a_handler_that_stops_the_script_ends_delivery_mid_batch(conn, fake):
    """Review H1 scenario A: a queued event for a script that stops mid-batch."""
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    calls, bus = [], []
    ctx.events.subscribe(UiEvent, bus.append)

    def first(e):
        calls.append(("first", e.current))
        ctx.stop_self()   # the stop is requested: nothing more reaches this script

    button.on_pressed(first)
    button.on(UiHook.ONBUTTONPRESSED, lambda e: calls.append(("second", e.current)))
    _route(ctx, _raw(button.handle, "onbuttonpressed", current=1, previous=0),
           _raw(button.handle, "onbuttonpressed", current=0, previous=1))
    assert calls == [("first", True)] and bus == []


def test_no_event_is_delivered_once_a_stop_is_requested_or_after_release(conn, fake):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    got = []
    button.on_pressed(got.append)
    ctx.events.subscribe(UiEvent, got.append)
    ctx.stop_self()
    _route(ctx, _raw(button.handle, "onbuttonpressed", current=1, previous=0))
    assert got == []
    ctx._release_ui()
    assert ctx.ui._handlers == []   # release clears them
    _route(ctx, _raw(button.handle, "destroyed", "removed"))
    assert got == []


def test_a_handler_that_destroys_its_component_drops_the_planned_rest(conn, fake):
    """Review H1 scenario B: the second event for the component is already planned."""
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    calls, bus = [], []
    ctx.events.subscribe(UiEvent, bus.append)

    def destroy_it(e):
        calls.append(e.current)
        button.destroy()

    button.on_pressed(destroy_it)
    _route(ctx, _raw(button.handle, "onbuttonpressed", current=1, previous=0),
           _raw(button.handle, "onbuttonpressed", current=0, previous=1))
    assert calls == [True] and bus == []   # not the second, and not to the bus either


def test_an_event_planned_with_its_destroyed_in_one_batch_is_dropped_but_destroyed_arrives(
        conn, fake):
    ctx = _ctx(conn)
    box = ctx.ui.list(ctx.ui.component(*GAME_AREA), [(1, "a")])
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    _route(ctx, _raw(box.handle, "onselectionchanged", "selected", entry_id=1, selection_event=1),
           _raw(box.handle, "destroyed", "removed"))
    assert [e.hook for e in got] == ["destroyed"]


def test_off_during_delivery_stops_a_handler_already_snapshotted(conn, fake):
    """Review M1."""
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    calls = []
    offs = {}

    def first(e):
        calls.append("first")
        offs["second"]()

    offs["first"] = button.on_pressed(first)
    offs["second"] = button.on_pressed(lambda e: calls.append("second"))
    _route(ctx, _raw(button.handle, "onbuttonpressed", current=1, previous=0))
    assert calls == ["first"]
    assert fake.verbs("unsubscribe") == []   # "first" still holds the hook


def test_close_delivery_waits_for_a_handler_in_flight(conn, fake):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    entered, release, order = threading.Event(), threading.Event(), []

    def slow(e):
        entered.set()
        release.wait(EVENT_WAIT_S)
        order.append("handler done")

    button.on_pressed(slow)
    t = threading.Thread(target=_route, args=(ctx, _raw(button.handle, "onbuttonpressed",
                                                        current=1, previous=0)))
    t.start()
    assert entered.wait(EVENT_WAIT_S)
    closer = threading.Thread(target=lambda: (ctx._close_ui_delivery(), order.append("closed")))
    closer.start()
    time.sleep(QUIET_S)
    assert order == []                     # still waiting for the handler
    release.set()
    t.join(EVENT_WAIT_S)
    closer.join(EVENT_WAIT_S)
    assert order == ["handler done", "closed"]


@script_manifest(name="OnStopRace")
class OnStopRace(BotScript):
    """Records when its handler runs and when on_stop starts."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.log: list[str] = []   # one ordered log: handler runs and on_stop's start

    def on_start(self, ctx) -> None:
        self.button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
        self.button.on_pressed(lambda e: self.log.append("handler"))
        self.ready.set()

    def on_loop(self) -> int:
        return 20

    def on_stop(self) -> None:
        self.log.append("on_stop")
        time.sleep(ON_STOP_S)   # long enough for pushes to land while on_stop runs


def test_no_handler_runs_during_or_after_on_stop(conn, fake):
    s = OnStopRace()
    conn.start_script(s)
    assert s.ready.wait(EVENT_WAIT_S)
    host, done = fake.hosts[0], threading.Event()

    def press_forever():
        while not done.is_set():
            fake.push(host, s.button.handle, "onbuttonpressed", "", current=1, previous=0)
            time.sleep(0.002)

    presser = threading.Thread(target=press_forever)
    presser.start()
    assert wait_until(lambda: "handler" in s.log)
    conn.stop_script("OnStopRace")
    time.sleep(QUIET_S)
    done.set()
    presser.join(EVENT_WAIT_S)
    assert s.log.count("on_stop") == 1
    assert "handler" not in s.log[s.log.index("on_stop"):]


# --- L1, L2 and nits ----------------------------------------------------------------------------

def test_a_content_set_made_before_subscribing_can_still_arrive(conn, fake):
    """The client raises oncontentchanged after ui.set returns (seen live on 951-4)."""
    fake.defer_content = True
    ctx = _ctx(conn)
    field = ctx.ui.input_field(ctx.ui.component(*GAME_AREA))
    got = []
    field.set_content("before")                  # the client has not run it yet
    field.on_content_changed(got.append)         # subscribed after the set returned
    fake.client_tick(fake.hosts[0])              # now the game thread raises it
    assert wait_until(lambda: got)
    assert [(e.content, e.reason) for e in got] == [("before", UiContentReason.CONTENT_CHANGE)]


def test_a_deferred_content_set_with_no_subscription_raises_nothing(conn, fake):
    fake.defer_content = True
    ctx = _ctx(conn)
    field = ctx.ui.input_field(ctx.ui.component(*GAME_AREA))
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    field.set_content("nobody listens")
    fake.client_tick(fake.hosts[0])
    time.sleep(QUIET_S)
    assert got == []


def test_a_raising_ctx_events_handler_is_logged(conn, fake, caplog):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))

    def boom(e):
        raise RuntimeError("handler boom")

    ctx.events.subscribe(UiEvent, boom)
    with caplog.at_level("ERROR", logger="botwithus.events"):
        _route(ctx, _raw(button.handle, "onbuttonpressed", current=1, previous=0))
    logged = [r for r in caplog.records if r.name == "botwithus.events" and r.exc_info]
    assert logged and "handler boom" in str(logged[0].exc_info[1])


def test_on_refuses_a_hook_it_does_not_take_before_sending(conn, fake):
    ctx = _ctx(conn)
    button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
    with pytest.raises(ValueError, match="ondrag"):
        button.on("ondrag", print)
    assert fake.verbs("subscribe") == [] and ctx.ui._handlers == []


def test_slider_read_value_raises_without_a_value(conn, fake):
    ui = _ctx(conn).ui
    layer = ui.layer(ui.component(*GAME_AREA))
    with pytest.raises(RuntimeError, match="no slider value"):
        Slider(ui, {"handle": layer.handle}).read_value()


# --- review round 2: stopping from a handler, natural ends, the worker ---------------------------

PROMPT_S = 1.0          # a stop or close from a handler returns well inside the 5 s join
HANDLER_BUSY_S = 0.3    # a handler still running when the script ends on its own


class SelfStopper(BotScript):
    """A button whose handler runs ``action`` (stop_script or close on its own connection)."""

    def __init__(self, conn, action) -> None:
        self.conn, self.action = conn, action
        self.ready, self.stopped = threading.Event(), threading.Event()
        self.took: float | None = None
        self.result = None

    def on_start(self, ctx) -> None:
        self.button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
        self.button.on_pressed(self._pressed)
        self.ready.set()

    def _pressed(self, e) -> None:
        start = time.monotonic()
        self.result = self.action(self.conn)
        self.took = time.monotonic() - start

    def on_loop(self) -> int:
        return 20

    def on_stop(self) -> None:
        self.stopped.set()


@script_manifest(name="SelfStop")
class SelfStop(SelfStopper):
    pass


def _press_from_ui_thread(fake, script):
    fake.push(fake.hosts[0], script.button.handle, "onbuttonpressed", "", current=1, previous=0)


def test_stop_script_from_its_own_handler_returns_promptly(conn, fake):
    s = SelfStop(conn, lambda c: c.stop_script("SelfStop"))
    conn.start_script(s)
    assert s.ready.wait(EVENT_WAIT_S)
    _press_from_ui_thread(fake, s)
    assert wait_until(lambda: s.took is not None)
    assert s.took < PROMPT_S                       # no 5 s join on its own delivery
    assert s.stopped.wait(EVENT_WAIT_S)            # on_stop still runs, after the handler
    assert wait_until(lambda: conn.running_scripts() == [])


def test_close_from_a_handler_returns_promptly_and_still_detaches(conn, fake):
    s = SelfStop(conn, lambda c: c.close())
    conn.start_script(s)
    assert s.ready.wait(EVENT_WAIT_S)
    _press_from_ui_thread(fake, s)
    assert wait_until(lambda: s.took is not None)
    assert s.took < PROMPT_S and s.result is False   # deferred, not freed under the handler
    assert s.stopped.wait(EVENT_WAIT_S)
    assert wait_until(lambda: fake.detached)        # freed once the script and ui thread end
    assert fake.verbs("destroy") == [("destroy", s.button.handle)]   # released before detach


class NaturalEnd(BotScript):
    """Ends on its own (on_loop returns -1) while its handler is still running."""

    def __init__(self) -> None:
        self.ready, self.entered = threading.Event(), threading.Event()
        self.log: list[str] = []

    def on_start(self, ctx) -> None:
        self.button = ctx.ui.button(ctx.ui.component(*GAME_AREA))
        self.button.on_pressed(self._pressed)
        self.ready.set()

    def _pressed(self, e) -> None:
        self.entered.set()
        time.sleep(HANDLER_BUSY_S)
        self.log.append("handler done")

    def on_loop(self) -> int:
        return -1 if self.entered.is_set() else 20

    def on_stop(self) -> None:
        self.log.append("on_stop")


@script_manifest(name="NaturalEndRun")
class NaturalEndRun(NaturalEnd):
    pass


def test_a_natural_end_waits_for_the_handler_before_on_stop(conn, fake):
    """Review L-1: no stop is requested here, so only the close before on_stop orders this."""
    s = NaturalEndRun()
    conn.start_script(s)
    assert s.ready.wait(EVENT_WAIT_S)
    _press_from_ui_thread(fake, s)
    assert wait_until(lambda: len(s.log) == 2)
    assert s.log == ["handler done", "on_stop"]


@pytest.fixture
def worker_conn():
    f = FakeUiNative(ctl_mode=FakeUiNative.CTL_WORKER)
    native_mod.set_backend(f)
    c = Connection(f._pid, enable_navigation=False)
    yield f, c
    c.close()
    native_mod.set_backend(None)


class _Run:
    """The run log _drive writes crashes to."""

    def __init__(self) -> None:
        self.crashes: list[str] = []

    def crash(self, phase, iteration, exc) -> None:
        self.crashes.append(phase)


class FailsInStart(NaturalEnd):
    """on_start fails while its handler is still running."""

    def on_start(self, ctx) -> None:
        super().on_start(ctx)
        assert self.entered.wait(EVENT_WAIT_S)
        raise RuntimeError("on_start fails with a handler in flight")


@pytest.mark.parametrize("plugin_type,crashes", [(NaturalEnd, []), (FailsInStart, ["on_start"])])
def test_the_worker_waits_for_the_handler_before_on_stop(worker_conn, plugin_type, crashes):
    """Review L-2: worker._drive's close before on_stop, on the normal and start-failed path."""
    from botwithus import worker

    fake, conn = worker_conn
    plugin, run = plugin_type(), _Run()
    ctx = conn.new_context("worker", threading.Event())
    worker._report("attaching")                  # as _run_plugin does before _drive
    t = threading.Thread(target=worker._drive, args=(plugin, conn, ctx, {}, run))
    t.start()
    assert plugin.ready.wait(EVENT_WAIT_S)
    fake.push(fake.hosts[0], plugin.button.handle, "onbuttonpressed", "", current=1, previous=0)
    t.join(EVENT_WAIT_S)
    assert not t.is_alive()
    assert plugin.log == ["handler done", "on_stop"]
    assert run.crashes == crashes
