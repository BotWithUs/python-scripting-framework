"""Live plugin-UI smoke through THIS framework's API (``ctx.ui``), run in bwu_host:

    set PYTHONPATH=<this repo>\\src
    bwu_host --py tests\\live\\ui_smoke.py

Driven by ``ui_smoke_run.py``, which decides pass or fail. pytest does not collect it. Needs a client
in the world with an agent from NXTLibrary 122d8b3 or later, and bwu_host from native-scripting-host
8dcdf71 or later.

cleanup: two scripts, A and B, run on ONE Connection, so they share one agent pipe. Under
  TOPLEVEL_V2__GAME_AREA (1477:61), A makes a layer, a text, a canvas and a rectangle nested in the
  layer, draws on the canvas, reads it all back and checks the refusals; B makes a layer and a
  text. Both write their components' keys, then "ready=1". On <out>.go_a the script stops A only,
  and writes "a_stopped=1"; on <out>.go_b it stops B and closes the connection. The driver checks
  over separate connections that A's components went while B's stayed, and how they went.
event: one script makes a layer and waits for its UiEvent through ctx.events.subscribe.

BWU_SMOKE_BREAK=release disables the framework's per-script release, to show the driver fails.
Exits non-zero on any mismatch.
"""
import os
import sys
import threading
import time

from botwithus.api import BotScript, UiEvent, UiHook, rgba, script_manifest
from botwithus.api import ui as ui_api
from botwithus.api.ui import Component, PluginUi
from botwithus.core.connection import Connection
from botwithus.core.native import default_client_pid

IFACE, COMP = 1477, 61   # TOPLEVEL_V2__GAME_AREA
FORBIDDEN = "ui: prop forbidden on agent components (no op, drag or target configuration)"
READ_ONLY = "ui: handle is a Jagex component: read-only, usable only as a parent or with ui.get"
UNKNOWN = "ui: unknown handle (destroyed, invalidated or not yours)"
WAIT_S = 120
TICK_MS = 50
SMOKE_TEXT = "python-scripting-framework plugin-UI smoke"

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


def refused(label, exc_type, text, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        check(label, str(exc), text)
        return
    raise AssertionError(f"{label}: no {exc_type.__name__}")


def wait_file(suffix):
    deadline = time.monotonic() + WAIT_S
    while not os.path.exists(out_path + suffix):
        if time.monotonic() > deadline:
            raise AssertionError(f"no {suffix} signal")
        time.sleep(TICK_MS / 1000)


@script_manifest(name="SmokeA")
class SmokeA(BotScript):
    def on_start(self, ctx):
        ui = ctx.ui
        root = ui.component(IFACE, COMP)
        check("root jagex", root.is_jagex, True)
        note("a_root", root.handle)
        layer = ui.layer(root, x=40, y=40, width=320, height=140)
        text = ui.text(root, SMOKE_TEXT, x=48, y=48, width=300, height=20, rgba=rgba(255, 255, 0))
        canvas = ui.canvas(root, x=40, y=80, width=320, height=100)
        inner = ui.rectangle(layer, x=4, y=4, width=20, height=20, rgba=0x00FF00FF, fill=True)
        with canvas.draw() as d:
            d.rect(0, 0, 320, 100, rgba=0x20202080)
            d.outline(0, 0, 320, 100, thickness=2, rgba=0xFFFFFFFF)
            d.line(10, 10, 310, 90, thickness=3, rgba=0xFF0000FF)
        check("canvas elements", d.elements, 3)
        check("text content", text.read_text(), SMOKE_TEXT)
        back = canvas.get()
        check("canvas geometry", (back.x, back.width, back.num_elements, back.op_mask), (40, 320, 3, 0))
        check("owned", ctx.ui.owned(), [layer.handle, text.handle, canvas.handle, inner.handle])
        refused("forbidden prop", ValueError, FORBIDDEN, ui.layer, root, op_config=1)
        refused("jagex set", ValueError, READ_ONLY, Component(ui, {"handle": root.handle}).set, x=1)
        refused("type later", ui_api.UiRefused, "ui type not supported yet: sprite",
                ui.create, root, "sprite")
        text.set(text=SMOKE_TEXT + " (set)")
        check("text after set", text.read_text(), SMOKE_TEXT + " (set)")
        for name, comp in (("a_layer", layer), ("a_text", text), ("a_canvas", canvas), ("a_inner", inner)):
            note(f"{name}_key", comp.key)
            note(f"{name}_handle", comp.handle)
        note("a_ready", 1)

    def on_loop(self):
        return TICK_MS


@script_manifest(name="SmokeB")
class SmokeB(BotScript):
    def on_start(self, ctx):
        root = ctx.ui.component(IFACE, COMP)
        note("b_root", root.handle)
        layer = ctx.ui.layer(root, x=40, y=200, width=200, height=40)
        text = ctx.ui.text(layer, "script B", x=4, y=4, width=190, height=20, rgba=0xFFFFFFFF)
        for name, comp in (("b_layer", layer), ("b_text", text)):
            note(f"{name}_key", comp.key)
            note(f"{name}_handle", comp.handle)
        note("b_ready", 1)

    def on_loop(self):
        return TICK_MS


def wait_note(key):
    deadline = time.monotonic() + WAIT_S
    while True:
        with lines_lock:
            if any(line.startswith(key + "=") for line in lines):
                return
        if time.monotonic() > deadline:
            raise AssertionError(f"no {key}")
        time.sleep(TICK_MS / 1000)


def run_cleanup(conn):
    conn.start_script(SmokeA())
    conn.start_script(SmokeB())
    wait_note("a_ready")
    wait_note("b_ready")
    note("ready", 1)
    wait_file(".go_a")
    conn.stop_script("SmokeA")
    check("A stopped, B running", conn.running_scripts(), ["SmokeB"])
    note("a_stopped", 1)
    wait_file(".go_b")
    conn.stop_script("SmokeB")
    note("b_stopped", 1)


@script_manifest(name="SmokeEvent")
class SmokeEvent(BotScript):
    def on_start(self, ctx):
        self.ctx = ctx
        self.events = []
        self.threads = set()
        ctx.events.subscribe(UiEvent, self._on_event)
        self.root = ctx.ui.component(IFACE, COMP)
        self.layer = ctx.ui.layer(self.root, x=40, y=40, width=100, height=40)
        note("layer_handle", self.layer.handle)
        note("ready", 1)
        self.deadline = time.monotonic() + WAIT_S

    def _on_event(self, event):
        self.threads.add(threading.current_thread().name)
        self.events.append(event)

    def on_loop(self):
        mine = [e for e in self.events if e.handle == self.layer.handle]
        if not mine:
            if time.monotonic() > self.deadline:
                note("event_error", "timeout")
                return -1
            return TICK_MS
        time.sleep(1.0)   # let any event for the Jagex root arrive too
        ev = mine[0]
        note("event_threads", ",".join(sorted(self.threads)))
        note("all_events", ";".join(f"{e.handle}/{e.hook}/{e.reason}" for e in self.events))
        note("root_handle", self.root.handle)
        note("root_alive_after_event", int(self.root.alive))
        note("event_handle_ok", int(ev.handle == self.layer.handle))
        note("event_hook", ev.hook)
        note("event_reason", ev.reason)
        note("layer_alive_after_event", int(self.layer.alive))
        try:
            self.layer.destroy()
            note("after_event", "no refusal")
        except ui_api.UiRefused as exc:
            note("after_event", "refused" if str(exc) == UNKNOWN else f"refused: {exc}")
        return -1


def run_event(conn):
    conn.start_script(SmokeEvent())
    deadline = time.monotonic() + WAIT_S + 30
    while conn.running_scripts() and time.monotonic() < deadline:
        time.sleep(0.2)
    check("event script ended", conn.running_scripts(), [])


def main():
    if os.environ.get("BWU_SMOKE_BREAK") == "release":
        PluginUi.release = lambda self: 0   # the regression the driver must catch
        note("broken", "release")
    pid = int(os.environ.get("BWU_SMOKE_PID") or default_client_pid())
    conn = Connection(pid, enable_navigation=False)
    try:
        if os.environ.get("BWU_SMOKE_MODE", "cleanup") == "event":
            run_event(conn)
        else:
            run_cleanup(conn)
    finally:
        note("closed", int(conn.close()))
    if failures:
        raise AssertionError(", ".join(failures))
    note("done", 1)


try:
    main()
except AssertionError as exc:
    print(f"ui_smoke.py: {exc}", flush=True)
    sys.exit(1)
