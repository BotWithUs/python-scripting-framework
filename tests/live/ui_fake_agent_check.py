"""The plugin UI of THIS framework, in the real ``bwu_host``, against native's fake_agent.

The unit suite fakes ``botwithus._native``. This runs the real binding and the real host pipe
(native-scripting-host 8dcdf71 or later), against ``fake_agent`` answering the agent's ui.* verbs
with per-connection ownership, so the framework's own per-script tracking, release and event routing
are checked end to end, with no client.

    fake_agent --answer-rpc 1 --ui 1 --ui-keep 1 --seconds 60
    set PYTHONPATH=<this repo>\\src
    set BWU_FAKE_PID=<fake_agent pid>
    bwu_host --py tests\\live\\ui_fake_agent_check.py

Two scripts share ONE Connection (one pipe). A second, separate NativeHost is the observer: it asks
the fake for every component and its owner (fake_ui_all), the request log (fake_ui_log), and to
remove a component as the game would (fake_ui_kill), which pushes ui.event to the owner.

BWU_SMOKE_BREAK=release disables the framework's per-script release; the check must then fail.
Exits non-zero on any mismatch. Developer tooling: pytest does not collect it.
"""
import os
import sys
import threading
import time

from botwithus.api import BotScript, UiEvent, script_manifest
from botwithus.api import ui as ui_api
from botwithus.api.ui import PluginUi
from botwithus.core.connection import Connection
from botwithus.core.native import NativeHost

IFACE, COMP = 1477, 61
WAIT_S = 10
TICK_MS = 20
failures = []


def expect(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def wait_for(pred, timeout_s=WAIT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Maker(BotScript):
    """Makes a layer with a rectangle in it, and a text; records its UiEvents."""

    def __init__(self):
        self.ready = threading.Event()
        self.events = []
        self.handles = []

    def on_start(self, ctx):
        ctx.events.subscribe(UiEvent, self.events.append)
        root = ctx.ui.component(IFACE, COMP)
        layer = ctx.ui.layer(root, x=1, y=2, width=30, height=40)
        rect = ctx.ui.rectangle(layer, fill=True)
        text = ctx.ui.text(root, "hello", width=50, height=20)
        canvas = ctx.ui.canvas(root, width=10, height=10)
        with canvas.draw() as d:
            for i in range(130):   # two ui.canvas calls: 128 + 2
                d.rect(i % 10, 0, 1, 1, rgba=0xFF0000FF)
        self.elements = d.elements
        self.text_back = text.read_text()
        try:
            ctx.ui.create(root, "sprite")
            self.later = "no refusal"
        except ui_api.UiRefused as exc:
            self.later = str(exc)
        self.handles = [layer.handle, rect.handle, text.handle, canvas.handle]
        self.ready.set()

    def on_loop(self):
        return TICK_MS


@script_manifest(name="FakeA")
class MakerA(Maker):
    pass


@script_manifest(name="FakeB")
class MakerB(Maker):
    pass


def components(observer):
    return {int(c["handle"]): int(c["owner"]) for c in observer.rpc("fake_ui_all")["handles"]}


def main():
    if os.environ.get("BWU_SMOKE_BREAK") == "release":
        PluginUi.release = lambda self: 0
        print("BROKEN: release disabled")
    pid = int(os.environ["BWU_FAKE_PID"])
    observer = NativeHost.attach(pid)
    conn = Connection(pid, enable_navigation=False)
    a, b = MakerA(), MakerB()
    conn.start_script(a)
    conn.start_script(b)
    expect("both ready", a.ready.wait(WAIT_S) and b.ready.wait(WAIT_S), True)
    expect("canvas batch of 130 in two calls", a.elements, 130)
    expect("text readback", a.text_back, "hello")
    expect("a later type is the agent's refusal, verbatim", a.later, "ui type not supported yet: sprite")
    owned = components(observer)
    expect("all eight components live", all(h in owned for h in a.handles + b.handles), True)
    expect("one owner: the shared connection", len({owned[h] for h in a.handles + b.handles}), 1)

    # Routing: the game removes B's text; only B hears of it.
    observer.rpc("fake_ui_kill", {"handle": b.handles[2], "reason": "removed"})
    expect("B got its UiEvent", wait_for(lambda: b.events), True)
    time.sleep(0.2)
    expect("B's event", [(e.handle, e.hook, e.reason) for e in b.events], [(b.handles[2], "destroyed", "removed")])
    expect("A got nothing", a.events, [])

    conn.stop_script("FakeA")
    after_a = components(observer)
    expect("A's components gone after A stopped", [h for h in a.handles if h in after_a], [])
    expect("B's live components untouched", [h for h in (b.handles[0], b.handles[1], b.handles[3]) if h in after_a],
           [b.handles[0], b.handles[1], b.handles[3]])
    log = observer.rpc("fake_ui_log")
    conn_id = owned[a.handles[0]]
    mine = [e for e in log if int(e["conn"]) == conn_id]
    expect("no ui.destroy_all on the shared pipe", [e for e in mine if e["method"] == "ui.destroy_all"], [])
    # The layer's rectangle went with the layer only if the layer went first; newest first means
    # the canvas, text, rectangle, then the layer: one ui.destroy each.
    expect("A's release: one ui.destroy each, newest first",
           [int(e["params"]["handle"]) for e in mine if e["method"] == "ui.destroy"], list(reversed(a.handles)))

    conn.stop_script("FakeB")
    after_b = components(observer)
    expect("B's components gone after B stopped", [h for h in b.handles if h in after_b], [])
    log = observer.rpc("fake_ui_log")
    b_destroys = [int(e["params"]["handle"]) for e in log
                  if int(e["conn"]) == conn_id and e["method"] == "ui.destroy"][len(a.handles):]
    expect("B's release skipped the killed text (no destroy sent for it)",
           b_destroys, [b.handles[3], b.handles[1], b.handles[0]])
    expect("closed", conn.close(), True)
    observer.detach()
    if failures:
        print("\n".join(failures))
        sys.exit(1)
    print("ui_fake_agent_check: all ok")


main()
