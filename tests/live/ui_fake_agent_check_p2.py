"""Plugin UI P2 of THIS framework, in the real ``bwu_host``, against native's fake_agent.

The unit suite fakes ``botwithus._native``. This runs the real binding and the real host pipe
(native-scripting-host 7bb38a4 or later) against ``fake_agent --ui``, which serves the agent's P2
verbs (NXTLibrary a17edc4) and can raise a hook as a user's input would (``fake_ui_hook``), so
every hook's typed decode is checked through the real event path with no client.

    fake_agent --answer-rpc 1 --ui 1 --ui-keep 1 --seconds 60
    set PYTHONPATH=<this repo>\\src
    set BWU_FAKE_PID=<fake_agent pid>
    bwu_host --py tests\\live\\ui_fake_agent_check_p2.py

Two scripts share ONE Connection. A separate NativeHost is the observer: it raises hooks
(fake_ui_hook), focuses a field (fake_ui_focus), removes a component (fake_ui_kill), and reads
every component with its owner and hooks (fake_ui_all) and the request log (fake_ui_log).

BWU_SMOKE_BREAK=decode makes the typed decode drop every event's fields (the check must fail);
BWU_SMOKE_BREAK=release disables the per-script release (the check must fail).
Exits non-zero on any mismatch. Developer tooling: pytest does not collect it.
"""
import os
import sys
import threading
import time

from botwithus.api import (BotScript, UiButtonPressed, UiClick, UiContentChanged,
                           UiEntrySelectionChanged, UiEntry, UiEvent, UiHook, UiSliderChanged,
                           script_manifest)
from botwithus.api import events as events_mod
from botwithus.api.ui import PluginUi
from botwithus.core.connection import Connection
from botwithus.core.native import NativeHost

IFACE, COMP = 1477, 61
WAIT_S = 10
QUIET_S = 0.3
TICK_MS = 20
failures = []


def expect(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}", flush=True)
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def wait_for(pred, timeout_s=WAIT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Panelist(BotScript):
    """A panel of every interactive type, with typed handlers on each one's own hook."""

    def __init__(self):
        self.ready = threading.Event()
        self.typed = []      # (helper, event) from the typed helpers
        self.every = []      # every UiEvent, via ctx.events

    def on_start(self, ctx):
        ui = ctx.ui
        self.ui = ui
        ctx.events.subscribe(UiEvent, self.every.append)
        root = ui.component(IFACE, COMP)
        self.panel = ui.panel(root, vertical=True, width=220, height=300)
        self.button = ui.button(self.panel, "Go", can_toggle=True, width=200, height=30)
        self.check = ui.check(self.panel, width=30, height=30)
        self.slider = ui.slider(self.panel, min=0, max=100, step=10, ticks=10, value=20)
        self.combo = ui.combo_field(self.panel, [UiEntry(7, "seven"), (8, "eight")], selected=7)
        self.box = ui.list(self.panel, [(1, "Oak"), (2, "Willow"), (3, "Yew")], selection_limit=3)
        self.field = ui.input_field(self.panel, max_length=20)
        self.grid = ui.grid(self.panel)
        self.offs = {
            "pressed": self.button.on_pressed(lambda e: self.typed.append(("pressed", e))),
            "checked": self.check.on_pressed(lambda e: self.typed.append(("checked", e))),
            "slider": self.slider.on_changed(lambda e: self.typed.append(("slider", e))),
            "combo": self.combo.on_selection_changed(lambda e: self.typed.append(("combo", e))),
            "list": self.box.on_selection_changed(lambda e: self.typed.append(("list", e))),
            "content": self.field.on_content_changed(lambda e: self.typed.append(("content", e))),
            "click": self.button.on_click(lambda e: self.typed.append(("click", e))),
        }
        self.ready.set()

    def on_loop(self):
        return TICK_MS


@script_manifest(name="P2FakeA")
class PanelistA(Panelist):
    pass


@script_manifest(name="P2FakeB")
class PanelistB(Panelist):
    pass


def components(observer):
    return {int(c["handle"]): c for c in observer.rpc("fake_ui_all")["handles"]}


def hook(observer, comp, name, *args):
    return observer.rpc("fake_ui_hook", {"handle": comp.handle, "hook": name, "args": list(args)})


def check_readback(a):
    back = a.button.get()
    expect("button readback", (back.label, back.can_toggle, back.toggled, back.enabled),
           ("Go", True, False, True))
    a.button.set_toggled(True)
    expect("button toggled", a.button.get().toggled, True)
    a.check.set_checked(True)
    expect("check checked", a.check.is_checked(), True)
    a.slider.set_value(30)
    back = a.slider.get()
    expect("slider readback", (back.min, back.max, back.step, back.ticks, back.value), (0, 100, 10, 10, 30))
    expect("combo readback", (a.combo.read_selected(), [e.id for e in a.combo.get().entries]), (7, [7, 8]))
    a.box.select([2])
    back = a.box.get()
    expect("list readback", (back.selected, back.selection_limit, [e.selected for e in back.entries]),
           ((2,), 3, [False, True, False]))
    a.field.set_content("first")
    back = a.field.get(content=True)
    expect("input readback", (back.content, back.focused, back.max_length), ("first", False, 20))
    expect("input renders reported as the agent reads it", (back.renders, back.font_id, back.sprite_id),
           (False, 26, -1))
    expect("panel vertical", a.panel.get().vertical, True)
    expect("grid has no type keys", (a.grid.get().enabled, a.grid.get().vertical), (True, None))


def check_hooks(observer, a, b):
    a.typed.clear()
    a.every.clear()
    expect("onbuttonpressed pushed", hook(observer, a.button, "onbuttonpressed", 1, 0)["pushed"], True)
    hook(observer, a.check, "onbuttonpressed", 0, 1)
    hook(observer, a.slider, "onselectionchanged", 40, 1)
    hook(observer, a.combo, "onselectionchanged", 8, 1)
    hook(observer, a.box, "onselectionchanged", 3, 0)
    hook(observer, a.field, "oncontentchanged", 2, "typed")
    hook(observer, a.button, "onclick", 5, 6)
    expect("seven typed events", wait_for(lambda: len(a.typed) == 7), True)
    got = {name: ev for name, ev in a.typed}
    expect("pressed", (type(got["pressed"]), got["pressed"].current, got["pressed"].previous),
           (UiButtonPressed, True, False))
    expect("check pressed", (got["checked"].current, got["checked"].previous), (False, True))
    expect("slider", (type(got["slider"]), got["slider"].value), (UiSliderChanged, 40))
    expect("combo", (type(got["combo"]), got["combo"].entry_id, got["combo"].selected, got["combo"].reason),
           (UiEntrySelectionChanged, 8, True, "selected"))
    expect("list", (got["list"].entry_id, got["list"].selection_event, got["list"].reason),
           (3, 0, "deselected"))
    expect("content", (type(got["content"]), got["content"].reason, got["content"].reason_code,
                       got["content"].content, got["content"].is_submit),
           (UiContentChanged, "submit", 2, "typed", True))
    expect("click", (type(got["click"]), got["click"].x, got["click"].y), (UiClick, 5, 6))
    expect("ctx.events got all seven too, typed", sorted(type(e).__name__ for e in a.every),
           sorted(type(e).__name__ for _, e in a.typed))
    expect("B heard none of A's", [e for e in b.every if e.handle in owned_by(a)], [])


def owned_by(script):
    return set(script.ui.owned())


def check_sets_and_subscriptions(observer, a):
    a.typed.clear()
    a.slider.set_value(50)
    a.box.select([1, 3])
    a.combo.select(None)
    a.field.set_content("bwu p2")
    expect("content set raised a typed event", wait_for(lambda: a.typed), True)
    time.sleep(QUIET_S)
    expect("only the content set raised anything",
           [(n, e.reason, e.reason_code, e.content) for n, e in a.typed],
           [("content", "content_change", 0, "bwu p2")])
    expect("agent-side hooks of the button", sorted(components(observer)[a.button.handle]["hooks"]),
           ["onbuttonpressed", "onclick"])
    a.offs["click"]()
    expect("off() unsubscribed onclick", sorted(components(observer)[a.button.handle]["hooks"]),
           ["onbuttonpressed"])
    expect("a raised onclick is no longer pushed", hook(observer, a.button, "onclick", 1, 1)["pushed"], False)
    expect("unsubscribe_hook answers was-subscribed", a.check.unsubscribe_hook(UiHook.ONBUTTONPRESSED), True)
    expect("unsubscribe_hook again", a.check.unsubscribe_hook(UiHook.ONBUTTONPRESSED), False)
    expect("blur unfocused", a.field.blur(), False)
    observer.rpc("fake_ui_focus", {"handle": a.field.handle})
    expect("focused after a user's click", a.field.get().focused, True)
    expect("blur focused", a.field.blur(), True)
    observer.rpc("fake_ui_kill", {"handle": a.grid.handle, "reason": "removed"})
    expect("grid's destroyed arrived", wait_for(lambda: not a.grid.alive), True)
    before = len(observer.rpc("fake_ui_log"))
    expect("unsubscribe on a dropped handle is success", a.grid.unsubscribe_hook(UiHook.ONCLICK), False)
    expect("...and sent nothing", len(observer.rpc("fake_ui_log")), before)


def check_stop(observer, conn, a, b):
    handles = a.ui.owned()
    conn.stop_script("P2FakeA")
    after = components(observer)
    expect("A's components gone after A stopped", [h for h in handles if h in after], [])
    expect("B's untouched", all(h in after for h in b.ui.owned()), True)
    expect("B's hooks untouched", sorted(after[b.slider.handle]["hooks"]), ["onselectionchanged"])
    log = observer.rpc("fake_ui_log")
    conn_id = int(after[b.slider.handle]["owner"])
    mine = [e for e in log if int(e["conn"]) == conn_id]
    expect("no ui.destroy_all on the shared pipe", [e for e in mine if e["method"] == "ui.destroy_all"], [])
    unsubs = [int(e["params"]["handle"]) for e in mine if e["method"] == "ui.unsubscribe"]
    expect("the stop sent no ui.unsubscribe (only the explicit two before it)",
           unsubs, [a.button.handle, a.check.handle, a.check.handle])
    for off in a.offs.values():
        off()   # after stop: a no-op that never raises
    expect("off() after stop sent nothing", len(observer.rpc("fake_ui_log")), len(log))
    conn.stop_script("P2FakeB")
    expect("B's gone after B stopped", [h for h in b.ui.owned() if h in components(observer)], [])


def break_decode():
    def plain(cls, raw):
        return UiEvent(handle=int(raw["handle"]), hook=str(raw["hook"]))
    events_mod.UiEvent.from_native = classmethod(plain)
    print("BROKEN: typed decode disabled", flush=True)


def main():
    broken = os.environ.get("BWU_SMOKE_BREAK")
    if broken == "decode":
        break_decode()
    elif broken == "release":
        PluginUi.release = lambda self: 0
        print("BROKEN: release disabled", flush=True)
    pid = int(os.environ["BWU_FAKE_PID"])
    observer = NativeHost.attach(pid)
    conn = Connection(pid, enable_navigation=False)
    a, b = PanelistA(), PanelistB()
    conn.start_script(a)
    conn.start_script(b)
    expect("both ready", a.ready.wait(WAIT_S) and b.ready.wait(WAIT_S), True)
    expect("has_p2", a.ui.has_p2, True)
    try:
        check_readback(a)
        check_hooks(observer, a, b)
        check_sets_and_subscriptions(observer, a)
        check_stop(observer, conn, a, b)
    finally:
        conn.close()
    print(f"ui_fake_agent_check_p2: {len(failures)} failure(s)", flush=True)
    return 0 if not failures else 1


sys.exit(main())
