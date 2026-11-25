"""Live plugin-UI P2 smoke through THIS framework's API (``ctx.ui``), run in bwu_host:

    set PYTHONPATH=<this repo>\\src
    bwu_host --py tests\\live\\ui_smoke_p2.py

Driven by ``ui_smoke_run.py --mode p2``, which decides pass or fail. pytest does not collect it.
Needs a client in the world with an agent from NXTLibrary a17edc4 or later, and bwu_host from
native-scripting-host 7bb38a4 or later.

Two scripts on ONE Connection (one agent pipe). P2A makes a panel under TOPLEVEL_V2__GAME_AREA
(1477:61) with a button, a check, a slider, a list and an input_field; sets and reads back their
values; checks two refusals verbatim; subscribes onselectionchanged on the slider and the list and
oncontentchanged on the input_field with the typed helpers; then sets values again through the
script API. The content set must raise a typed UiContentChanged; the slider and list sets must
raise nothing (PUBLIC_PROTOCOL 6.12: a set never raises the change hook). P2B makes one button with
an onbuttonpressed subscription and must keep it when P2A stops. No click, no _debug verb.

On <out>.go_a the run stops P2A only, WITHOUT any unsubscribe or destroy of its own: the framework's
release must take the components and, with them, the subscriptions. On <out>.go_b it stops P2B and
closes the connection. Exits non-zero on any in-script mismatch.

BWU_SMOKE_BREAK=release disables the framework's per-script release, to show the driver fails.
"""
import os
import sys
import threading
import time

from botwithus.api import (BotScript, UiContentChanged, UiEvent, UiHook, UiSelectionChanged,
                           script_manifest)
from botwithus.api import ui as ui_api
from botwithus.api.ui import PluginUi
from botwithus.core.connection import Connection
from botwithus.core.native import default_client_pid

IFACE, COMP = 1477, 61   # TOPLEVEL_V2__GAME_AREA
WAIT_S = 120
EVENT_WAIT_S = 5
TICK_MS = 50
SMOKE_CONTENT = "bwu p2 smoke"
CHECK_LABEL = "ui prop not supported yet: label"   # the agent's text starts with this
HOOK_NOT_VALID = "ui: hook not valid for this type"

out_path = os.environ["BWU_SMOKE_OUT"]
lines = []
lines_lock = threading.Lock()
failures = []


def note(key, value):
    with lines_lock:
        lines.append(f"{key}={value}")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print(f"SMOKE {key}={value}", flush=True)


def check(label, got, want):
    if got != want:
        failures.append(label)
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def refused_text(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ui_api.UiRefused as exc:
        return str(exc)
    return "no refusal"


def wait_file(suffix):
    deadline = time.monotonic() + WAIT_S
    while not os.path.exists(out_path + suffix):
        if time.monotonic() > deadline:
            raise AssertionError(f"no {suffix} signal")
        time.sleep(TICK_MS / 1000)


@script_manifest(name="P2A")
class SmokeP2A(BotScript):
    def on_start(self, ctx):
        self.events = []
        self.threads = set()
        ctx.events.subscribe(UiEvent, self._on_event)
        ui = ctx.ui
        check("has_p2", ui.has_p2, True)
        root = ui.component(IFACE, COMP)
        c = self._build(ui, root)
        self._set_and_read(c)
        self._refusals(c)
        self._events_after_sets(c)
        note("blurred", int(c["input"].blur()))
        for name, comp in c.items():
            note(f"a_{name}_handle", comp.handle)
        note("a_panel_key", c["panel"].key)
        note("a_components", len(ui.owned()))
        note("a_ready", 1)

    def _on_event(self, event):
        self.threads.add(threading.current_thread().name)
        self.events.append(event)

    @staticmethod
    def _build(ui, root):
        panel = ui.panel(root, vertical=True, x=40, y=40, width=220, height=260)
        return {
            "panel": panel,
            "button": ui.button(panel, "bwu P2", can_toggle=True, width=200, height=30),
            "check": ui.check(panel, width=30, height=30),
            "slider": ui.slider(panel, min=0, max=100, step=10, ticks=10, value=20,
                                width=200, height=30),
            "list": ui.list(panel, [(1, "Oak"), (2, "Willow"), (3, "Yew")], selection_limit=3,
                            width=200, height=80),
            "input": ui.input_field(panel, width=200, height=30, max_length=20),
        }

    @staticmethod
    def _set_and_read(c):
        c["button"].set_toggled(True)
        c["check"].set_checked(True)
        c["slider"].set_value(30)
        c["list"].select([2])
        c["input"].set_content("first")
        check("panel vertical", c["panel"].get().vertical, True)
        r = c["button"].get()
        check("button", (r.label, r.toggled, r.enabled), ("bwu P2", True, True))
        check("check checked", c["check"].is_checked(), True)
        r = c["slider"].get()
        check("slider", (r.value, r.min, r.max, r.step, r.ticks), (30, 0, 100, 10, 10))
        r = c["list"].get()
        check("list", (len(r.entries), r.selected, r.entries[1].selected, r.selection_limit),
              (3, (2,), True, 3))
        r = c["input"].get(content=True)
        check("input", (r.content, r.focused, r.max_length), ("first", False, 20))
        note("input_renders", r.renders)   # reported as is: False is expected on 951-4
        note("input_font_id", r.font_id)
        note("input_sprite_id", r.sprite_id)

    @staticmethod
    def _refusals(c):
        text = refused_text(c["check"].set, label="x")
        note("check_label_refusal", text)
        check("check label refused verbatim", text.startswith(CHECK_LABEL), True)
        text = refused_text(c["slider"].subscribe_hook, UiHook.ONBUTTONPRESSED)
        check("slider onbuttonpressed refused verbatim", text, HOOK_NOT_VALID)
        check("a refused subscribe is not tracked", c["slider"].hooks(), frozenset())

    def _events_after_sets(self, c):
        typed = []
        c["slider"].on_changed(typed.append)
        c["list"].on_selection_changed(typed.append)
        c["input"].on_content_changed(typed.append)
        check("hooks tracked", sorted(h for k in ("slider", "list", "input") for h in c[k].hooks()),
              ["oncontentchanged", "onselectionchanged", "onselectionchanged"])
        self.events.clear()
        c["slider"].set_value(40)
        c["list"].select([1, 3])
        c["input"].set_content(SMOKE_CONTENT)
        check("slider value 2", c["slider"].read_value(), 40)
        check("list selected 2", c["list"].read_selected(), (1, 3))
        time.sleep(EVENT_WAIT_S)
        content = [e for e in typed if isinstance(e, UiContentChanged)]
        selections = [e for e in self.events if isinstance(e, UiSelectionChanged)]
        note("content_events", ";".join(f"{e.handle}/{e.reason}/{e.reason_code}/{e.content!r}"
                                        for e in content))
        note("content_events_ctx", sum(isinstance(e, UiContentChanged) for e in self.events))
        # The client raises oncontentchanged on the game thread after the set returns, so the
        # "first" set just before the subscribe can arrive too (observed live on 951-4): every
        # event must be one of this script's own sets, on its field, and the last one the smoke's.
        check("content events are the script's own sets",
              all(e.handle == c["input"].handle and e.content in ("first", SMOKE_CONTENT)
                  for e in content) and len(content) >= 1, True)
        check("content events reached ctx.events once each",
              sum(isinstance(e, UiContentChanged) for e in self.events), len(content))
        ev = content[-1]
        note("content_type", type(ev).__name__)
        note("content_handle_ok", int(ev.handle == c["input"].handle))
        note("content_reason", ev.reason)
        note("content_reason_code", ev.reason_code)
        note("content_text", ev.content)
        note("content_in_ctx_events", int(any(e is ev for e in self.events)))
        note("selection_events", len(selections))
        note("event_threads", ",".join(sorted(self.threads)))

    def on_loop(self):
        return TICK_MS


@script_manifest(name="P2B")
class SmokeP2B(BotScript):
    def on_start(self, ctx):
        root = ctx.ui.component(IFACE, COMP)
        button = ctx.ui.button(root, "script B", x=300, y=40, width=120, height=30)
        button.on_pressed(lambda e: None)
        check("B's hook", button.hooks(), frozenset({UiHook.ONBUTTONPRESSED}))
        note("b_button_key", button.key)
        note("b_button_handle", button.handle)
        note("b_ready", 1)

    def on_loop(self):
        return TICK_MS


def wait_note(key):
    deadline = time.monotonic() + WAIT_S
    while True:
        with lines_lock:
            if any(line.startswith(key + "=") for line in lines):
                return
        if failures:
            raise AssertionError(", ".join(failures))
        if time.monotonic() > deadline:
            raise AssertionError(f"no {key}")
        time.sleep(TICK_MS / 1000)


def main():
    if os.environ.get("BWU_SMOKE_BREAK") == "release":
        PluginUi.release = lambda self: 0   # the regression the driver must catch
        note("broken", "release")
    pid = int(os.environ.get("BWU_SMOKE_PID") or default_client_pid())
    conn = Connection(pid, enable_navigation=False)
    try:
        conn.start_script(SmokeP2A())
        conn.start_script(SmokeP2B())
        wait_note("a_ready")
        wait_note("b_ready")
        note("ready", 1)
        wait_file(".go_a")
        conn.stop_script("P2A")   # no unsubscribe, no destroy: the release must take it all
        check("A stopped, B running", conn.running_scripts(), ["P2B"])
        note("a_stopped", 1)
        wait_file(".go_b")
        conn.stop_script("P2B")
        note("b_stopped", 1)
    finally:
        note("closed", int(conn.close()))
    if failures:
        raise AssertionError(", ".join(failures))
    note("done", 1)


try:
    main()
except AssertionError as exc:
    print(f"ui_smoke_p2.py: {exc}", flush=True)
    sys.exit(1)
