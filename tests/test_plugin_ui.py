"""Plugin UI (``ctx.ui``, ``botwithus.api.ui``) against a fake of the native host's ``ui_*``
surface: the verbs, error passthrough, per-script release on stop on a shared pipe, and ui event
routing to the owning script."""
from __future__ import annotations

import struct
import threading
import time

import pytest

from botwithus.api import BotScript, script_manifest
from botwithus.api import ui as ui_mod
from botwithus.api.events import UiEvent, UiHook
from botwithus.api.ui import Canvas, ComponentType, JagexComponent, Layer, Text, rgba
from botwithus.api.events import ServerTick
from botwithus.core import layout as L
from botwithus.core import native as native_mod
from botwithus.core.connection import Connection
from botwithus.core.native import ClientExited

from fake_ui import (BAD_CATEGORY, FORBIDDEN, UNKNOWN_HANDLE, FakeUiNative, FakeUiRefused)

GAME_AREA = (1477, 61)   # TOPLEVEL_V2__GAME_AREA
EVENT_WAIT_S = 2.0
POLL_S = 0.01


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


# --- verbs -------------------------------------------------------------------------------------

def test_typed_creates_send_the_verb_and_come_back_as_their_wrapper(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    assert isinstance(root, JagexComponent) and root.is_jagex
    layer = ui.layer(root, x=40, y=40, width=320, height=140)
    text = ui.text(root, "hi", x=48, rgba=rgba(255, 255, 0))
    canvas = ui.canvas(layer, width=10, height=10, category=7, dynamic=3)
    assert (type(layer), type(text), type(canvas)) == (Layer, Text, Canvas)
    assert fake.verbs("create") == [
        ("create", root.handle, "layer", {"x": 40, "y": 40, "width": 320, "height": 140}, None, None),
        ("create", root.handle, "text", {"text": "hi", "x": 48, "rgba": 0xFFFF00FF}, None, None),
        ("create", layer.handle, "canvas", {"width": 10, "height": 10}, 7, 3),
    ]
    assert ui.owned() == [layer.handle, text.handle, canvas.handle]   # not the Jagex lookup


def test_create_takes_any_type_name_so_a_later_type_needs_no_change(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    rect = ui.create(root, ComponentType.RECTANGLE, fill=True)
    assert type(rect).__name__ == "Rectangle"
    with pytest.raises(FakeUiRefused) as err:
        ui.create(root, "sprite", x=1)                    # still refused at NXTLibrary a17edc4
    assert str(err.value) == "ui type not supported yet: sprite"
    assert fake.verbs("create")[-1][2] == "sprite"


def test_set_get_order_and_destroy(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    text = ui.text(root, "one", x=5, y=6, width=50, height=20)
    text.set(text="two", rgba=0x112233FF)
    back = text.get(content=True)
    assert (back.x, back.y, back.width, back.rgba, back.content) == (5, 6, 50, 0x112233FF, "two")
    assert back.handle == text.handle and back.jagex is False
    assert text.get().content is None
    assert text.read_text() == "two"
    text.to_front()
    text.order("back")
    assert fake.verbs("order") == [("order", text.handle, "front"), ("order", text.handle, "back")]
    assert text.alive
    assert text.destroy() is True
    assert not text.alive and ui.owned() == []
    root_back = root.get()
    assert root_back.jagex is True


def test_canvas_batch_is_one_call_with_the_protocol_op_shapes(conn, fake):
    ui = _ctx(conn).ui
    canvas = ui.canvas(ui.component(*GAME_AREA), width=320, height=100)
    with canvas.draw() as d:
        d.rect(0, 0, 320, 100, rgba=0x20202080)
        d.outline(0, 0, 320, 100, thickness=2, rgba=0xFFFFFFFF)
        d.line(10, 10, 310, 90, thickness=3, rgba=0xFF0000FF)
        d.gradient_h(1, 2, 3, 4, rgba_start=1, rgba_end=2)
        d.gradient_v(1, 2, 3, 4, rgba_start=1, rgba_end=2)
        d.triangle(0, 0, 5, 0, 0, 5, rgba=9)
    assert d.elements == 6
    (call,) = fake.verbs("canvas")
    assert call[2] == [
        {"op": "rect", "x": 0, "y": 0, "width": 320, "height": 100, "rgba": 0x20202080},
        {"op": "outline", "x": 0, "y": 0, "width": 320, "height": 100, "thickness": 2, "rgba": 0xFFFFFFFF},
        {"op": "line", "x": 10, "y": 10, "end_x": 310, "end_y": 90, "thickness": 3, "rgba": 0xFF0000FF},
        {"op": "gradient_h", "x": 1, "y": 2, "width": 3, "height": 4, "rgba_start": 1, "rgba_end": 2},
        {"op": "gradient_v", "x": 1, "y": 2, "width": 3, "height": 4, "rgba_start": 1, "rgba_end": 2},
        {"op": "triangle", "x0": 0, "y0": 0, "x1": 5, "y1": 0, "x2": 0, "y2": 5, "rgba": 9},
    ]
    assert canvas.undo() == 5
    assert canvas.clear() == 0


def test_a_long_canvas_batch_goes_in_chunks_of_128(conn, fake):
    ui = _ctx(conn).ui
    canvas = ui.canvas(ui.component(*GAME_AREA))
    batch = canvas.draw()
    for i in range(300):
        batch.rect(i, 0, 1, 1, rgba=0)
    assert batch.submit() == 300
    assert [len(c[2]) for c in fake.verbs("canvas")] == [128, 128, 44]
    with pytest.raises(RuntimeError):
        batch.submit()


def test_a_canvas_batch_is_not_sent_when_its_block_raises(conn, fake):
    ui = _ctx(conn).ui
    canvas = ui.canvas(ui.component(*GAME_AREA))
    with pytest.raises(KeyError):
        with canvas.draw() as d:
            d.rect(0, 0, 1, 1, rgba=0)
            raise KeyError("x")
    assert fake.verbs("canvas") == []
    assert canvas.draw().submit() == -1 and fake.verbs("canvas") == []


def test_rgba_packs_and_range_checks():
    assert rgba(0x12, 0x34, 0x56, 0x78) == 0x12345678
    assert rgba(1, 2, 3) == 0x010203FF
    with pytest.raises(ValueError):
        rgba(256, 0, 0)


# --- errors pass through as the host raises them ---------------------------------------------

def test_refusals_reach_the_script_verbatim_and_unwrapped(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    layer = ui.layer(root)
    with pytest.raises(ValueError) as forbidden:
        ui.layer(root, op_config=1)
    assert str(forbidden.value) == FORBIDDEN
    with pytest.raises(ValueError) as category:
        ui.layer(root, category=0)
    assert str(category.value) == BAD_CATEGORY
    layer.destroy()
    with pytest.raises(FakeUiRefused) as gone:
        layer.set(x=1)
    assert str(gone.value) == UNKNOWN_HANDLE
    assert type(gone.value) is FakeUiRefused


def test_ui_refused_is_the_native_hosts_own_class(fake):
    assert ui_mod.UiRefused is fake.UiRefused
    from botwithus.api.ui import UiRefused
    assert UiRefused is FakeUiRefused and issubclass(UiRefused, RuntimeError)


def test_a_jagex_handle_offers_no_edit(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    for edit in ("set", "order", "to_front", "to_back", "destroy"):
        assert not hasattr(root, edit), edit
    assert root.alive and root.get().jagex is True
    assert ui.owned() == []               # a lookup, not a component the script made


def test_list_is_the_list_component_and_handles_is_the_listing(conn, fake):
    ui = _ctx(conn).ui
    assert type(ui.list(ui.component(*GAME_AREA))).__name__ == "ListBox"   # P2 took the name
    assert [h["type_name"] for h in ui.handles()] == ["layer", "list"]       # the Jagex lookup too


def test_timeout_passes_through_and_a_lost_client_is_client_exited(conn, fake):
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    fake.fail_next["create"] = TimeoutError("rpc_timeout")
    with pytest.raises(TimeoutError) as err:
        ui.layer(root)
    assert str(err.value) == "rpc_timeout"
    fake.client_gone = True
    with pytest.raises(ClientExited):
        ui.layer(root)


def test_a_malformed_reply_passes_through_as_runtime_error(conn, fake):
    ui = _ctx(conn).ui
    fake.fail_next["list"] = RuntimeError("ui.list: malformed reply: handles")
    with pytest.raises(RuntimeError) as err:
        ui.handles()
    assert str(err.value) == "ui.list: malformed reply: handles"
    assert conn.alive   # not mistaken for a dead transport


# --- per-script release on a shared pipe -------------------------------------------------------

@script_manifest(name="Painter")
class Painter(BotScript):
    """Makes a layer, a nested rectangle and a text, then idles until stopped."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.made: list[int] = []
        self.ready = threading.Event()
        self.stopped_with: list[int] = []

    def on_start(self, ctx) -> None:
        self.ctx = ctx
        root = ctx.ui.component(*GAME_AREA)
        layer = ctx.ui.layer(root, x=1)
        rect = ctx.ui.rectangle(layer, fill=True)
        text = ctx.ui.text(root, self.tag)
        self.made = [layer.handle, rect.handle, text.handle]
        self.ready.set()

    def on_loop(self) -> int:
        return 20

    def on_stop(self) -> None:
        self.stopped_with = self.ctx.ui.owned()   # on_stop still sees its components


@script_manifest(name="A")
class PainterA(Painter):
    pass


@script_manifest(name="B")
class PainterB(Painter):
    pass


def _two_scripts(conn):
    a, b = PainterA("a"), PainterB("b")
    conn.start_script(a)
    conn.start_script(b)
    assert a.ready.wait(EVENT_WAIT_S) and b.ready.wait(EVENT_WAIT_S)
    return a, b


def test_stopping_one_script_destroys_exactly_its_components(conn, fake):
    a, b = _two_scripts(conn)
    (host,) = fake.hosts                     # one attach: both scripts share one pipe
    conn.stop_script("A")
    assert a.stopped_with == a.made
    destroyed = [c[1] for c in fake.verbs("destroy")]
    assert destroyed == list(reversed(a.made))          # newest first, one ui.destroy each
    assert fake.verbs("destroy_all") == []              # never on a shared pipe
    alive = fake.agent_components(host)
    assert all(h not in alive for h in a.made)
    assert all(h in alive for h in b.made)               # B's UI untouched
    conn.stop_script("B")
    assert sorted(c[1] for c in fake.verbs("destroy")) == sorted(a.made + b.made)
    assert fake.verbs("destroy_all") == []


def test_release_skips_what_the_agent_already_took_and_never_raises(conn, fake):
    ctx = _ctx(conn)
    ui = ctx.ui
    root = ui.component(*GAME_AREA)
    layer = ui.layer(root)
    child = ui.rectangle(layer)          # goes with its parent, with no event
    other = ui.text(root, "x")
    fake.push(fake.hosts[0], other.handle, "destroyed", "parent_gone")
    assert wait_until(lambda: not other.alive)   # the event took it off the script's list
    layer.destroy()
    assert ui.owned() == [child.handle]
    ctx._release_ui()                    # child: UiRefused unknown handle -> skipped
    assert [c[1] for c in fake.verbs("destroy")] == [layer.handle, child.handle]
    assert ui.owned() == [] and ui.closed
    ctx._release_ui()                    # idempotent: nothing more is sent
    assert len(fake.verbs("destroy")) == 2


def test_release_stops_at_a_timeout_and_leaves_the_rest_to_the_host(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    made = [ctx.ui.layer(root).handle for _ in range(3)]
    fake.fail_next["destroy"] = TimeoutError("rpc_timeout")
    ctx._release_ui()
    assert [c[1] for c in fake.verbs("destroy")] == [made[-1]]   # bounded: one try, then stop
    conn.close()
    assert fake.verbs("host_release") == [("host_release", 3)]   # the backstop took the rest


def test_a_crashing_script_is_released_too(conn, fake):
    @script_manifest(name="Crash")
    class Crash(Painter):
        def on_loop(self) -> int:
            raise RuntimeError("boom")

    s = Crash("c")
    conn.start_script(s)
    assert s.ready.wait(EVENT_WAIT_S)
    assert wait_until(lambda: len(fake.verbs("destroy")) == 3)
    assert sorted(c[1] for c in fake.verbs("destroy")) == sorted(s.made)


# --- events ------------------------------------------------------------------------------------

def test_ui_events_reach_only_the_owning_script(conn, fake):
    a, b = _two_scripts(conn)
    got_a, got_b = [], []
    a.ctx.events.subscribe(UiEvent, got_a.append)
    b.ctx.events.subscribe(UiEvent, got_b.append)
    fake.push(fake.hosts[0], a.made[2], "destroyed", "parent_gone")
    assert wait_until(lambda: got_a)
    time.sleep(0.1)
    assert got_b == []
    (ev,) = got_a
    assert (ev.handle, ev.hook, ev.reason, ev.is_destroyed) == (a.made[2], "destroyed", "parent_gone", True)
    assert a.made[2] not in a.ctx.ui.owned()
    fake.push(fake.hosts[0], b.made[0], "destroyed", "server_events")
    assert wait_until(lambda: got_b)
    assert [e.handle for e in got_a] == [a.made[2]]


def test_overflow_reaches_every_script_using_the_ui(conn, fake):
    a, b = _two_scripts(conn)
    got_a, got_b = [], []
    a.ctx.events.subscribe(UiEvent, got_a.append)
    b.ctx.events.subscribe(UiEvent, got_b.append)
    fake.drop_pushes(fake.hosts[0], 7)
    assert wait_until(lambda: got_a and got_b)
    for got in (got_a, got_b):
        (ev,) = got
        assert (ev.handle, ev.hook, ev.is_overflow, ev.dropped) == (0, UiHook.OVERFLOW, True, 7)


def test_ui_subscribe_filters_by_handle_and_hook_and_unsubscribes(conn, fake):
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    one, two = ctx.ui.layer(root), ctx.ui.layer(root)
    by_handle, by_hook, later = [], [], []
    off = one.subscribe(by_handle.append)
    ctx.ui.subscribe(by_hook.append, hook=UiHook.DESTROYED)
    ctx.ui.subscribe(later.append, hook="clicked")
    fake.push(fake.hosts[0], two.handle, "clicked", "", button=1)   # a P2-style hook, generic
    assert wait_until(lambda: later)
    assert later[0].fields == {"button": 1} and by_handle == [] and by_hook == []
    off()
    fake.push(fake.hosts[0], one.handle, "destroyed", "removed")
    assert wait_until(lambda: by_hook)
    assert by_handle == [] and by_hook[0].handle == one.handle


def test_an_event_for_a_released_script_is_dropped(conn, fake):
    ctx = _ctx(conn)
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    root = ctx.ui.component(*GAME_AREA)
    ctx.ui.layer(root)
    ctx._release_ui()
    fake.drop_pushes(fake.hosts[0], 1)
    time.sleep(0.2)
    assert got == []


def test_ui_events_are_polled_only_once_a_script_used_the_ui(conn, fake):
    time.sleep(0.15)
    assert fake.polls == 0
    _ctx(conn).ui.component(*GAME_AREA)
    assert wait_until(lambda: fake.polls > 0)


def test_game_events_still_go_through_the_shared_bus(conn, fake):
    from botwithus.api.events import ServerTick
    a, b = _ctx(conn, "a"), _ctx(conn, "b")
    got = []
    a.events.subscribe(ServerTick, got.append)
    b.events.publish(ServerTick(seq=1, tick=5))
    conn.events.publish(ServerTick(seq=2, tick=6))
    assert [e.tick for e in got] == [5, 6]


def test_a_lost_client_stops_ui_polling_without_killing_the_pump(conn, fake):
    _ctx(conn).ui.component(*GAME_AREA)
    assert wait_until(lambda: fake.polls > 0)
    fake.client_gone = True
    time.sleep(0.15)
    polls = fake.polls
    time.sleep(0.15)
    assert fake.polls == polls          # stopped: no 20 ms error loop
    assert conn._pump.is_alive()


# --- the GUI worker path (one script per connection) ---------------------------------------

WORKER_PLUGIN = """
    import ui_trace as T
    from botwithus.api import BotScript, script_manifest

    @script_manifest(name="UiWorker")
    class UiWorker(BotScript):
        def on_start(self, ctx):
            self.ctx = ctx
            root = ctx.ui.component(1477, 61)
            T.made.append(ctx.ui.layer(root, x=1).handle)

        def on_loop(self):
            return -1

        def on_stop(self):
            T.calls_at_stop.extend(T.fake.calls)
"""


def test_a_worker_run_releases_its_components_before_the_host_detaches(tmp_path, monkeypatch):
    import sys
    import textwrap
    import types

    from botwithus import worker

    trace = types.ModuleType("ui_trace")
    trace.made, trace.calls_at_stop = [], []
    trace.fake = fake = FakeUiNative(ctl_mode=FakeUiNative.CTL_WORKER)
    monkeypatch.setitem(sys.modules, "ui_trace", trace)
    monkeypatch.setattr(sys, "path", list(sys.path))
    native_mod.set_backend(fake)
    try:
        path = tmp_path / "ui_worker.py"
        path.write_text(textwrap.dedent(WORKER_PLUGIN), encoding="utf-8")
        worker.run(str(path), fake._pid)
    finally:
        sys.modules.pop("bwu_script", None)
        native_mod.set_backend(None)
    (made,) = trace.made
    assert ("destroy", made) not in trace.calls_at_stop          # on_stop ran first
    order = [c[0] for c in fake.calls if c[0] in ("destroy", "host_release", "destroy_all")]
    assert order == ["destroy", "host_release"]                  # framework, then host backstop
    assert fake.verbs("destroy") == [("destroy", made)]
    assert fake.crashes == []


# --- review round 1 -------------------------------------------------------------------------

def _tick_record(tick: int) -> dict:
    return {"type": L.EVT_TICK, "body": struct.pack("<i", tick)}


def test_game_events_keep_flowing_while_a_ui_call_holds_the_host_lock(conn, fake):
    """F1: ui_poll_events waits on the host's I/O lock. Polled on the ring pump, one script's
    slow ui call would stall every script's ServerTick, chat and varp events."""
    ticks = []
    conn.events.subscribe(ServerTick, ticks.append)
    ui = _ctx(conn).ui
    root = ui.component(*GAME_AREA)
    assert wait_until(lambda: fake.polls > 0)       # the ui poll is running
    fake.block_verb = "create"
    maker = threading.Thread(target=ui.layer, args=(root,), daemon=True)
    maker.start()
    try:
        assert fake.blocked.wait(EVENT_WAIT_S)      # a ui call now holds the lock
        fake.hosts[0].events.append(_tick_record(77))
        assert wait_until(lambda: ticks, 1.0), "the ring pump stalled behind a ui call"
        assert ticks[0].tick == 77
    finally:
        fake.unblock.set()
        maker.join(EVENT_WAIT_S)


def test_a_destroyed_event_that_races_create_still_reaches_the_script(conn, fake):
    """F2 (the reviewer's reproduction): the agent invalidates a new component before create
    returns; the event is polled while the script does not hold the handle yet."""
    ctx = _ctx(conn)
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    root = ctx.ui.component(*GAME_AREA)
    assert wait_until(lambda: fake.polls > 0)
    fake.race_create = "server_events"
    layer = ctx.ui.layer(root)
    assert wait_until(lambda: got)
    assert [(e.handle, e.hook, e.reason) for e in got] == [(layer.handle, "destroyed", "server_events")]
    assert not layer.alive and ctx.ui.owned() == []


def test_events_for_handles_nobody_claims_expire():
    from botwithus.api import ui as ui_api
    now = [0.0]
    router = ui_api.UiRouter(clock=lambda: now[0])
    router.route([{"handle": 9, "hook": "destroyed", "reason": "removed"}])
    assert router.pending_count() == 1
    now[0] += ui_api.PENDING_TTL_S + 1
    router.route([])
    assert router.pending_count() == 0
    for h in range(ui_api.PENDING_MAX + 10):
        router.route([{"handle": 1000 + h, "hook": "destroyed"}])
    assert router.pending_count() == ui_api.PENDING_MAX


def test_jagex_events_reach_every_script_that_looked_it_up(conn, fake):
    """F3: the agent dedups lookups per connection, so two scripts share one Jagex handle;
    its destroyed/removed event must reach both, not be dropped."""
    a, b = _ctx(conn, "a"), _ctx(conn, "b")
    got_a, got_b = [], []
    a.events.subscribe(UiEvent, got_a.append)
    b.events.subscribe(UiEvent, got_b.append)
    root_a, root_b = a.ui.component(*GAME_AREA), b.ui.component(*GAME_AREA)
    assert root_a.handle == root_b.handle          # one handle per connection
    fake.push(fake.hosts[0], root_a.handle, "destroyed", "removed")
    assert wait_until(lambda: got_a and got_b)
    for got in (got_a, got_b):
        assert [(e.handle, e.reason) for e in got] == [(root_a.handle, "removed")]
    assert not root_a.alive and not root_b.alive


def test_a_long_canvas_batch_reports_how_much_was_applied_when_a_chunk_fails(conn, fake):
    """F5: a batch over 128 ops is sent in chunks and is not atomic."""
    ui = _ctx(conn).ui
    canvas = ui.canvas(ui.component(*GAME_AREA))
    batch = canvas.draw()
    for i in range(129):
        batch.rect(i, 0, 1, 1, rgba=0)
    batch.op("sprite", x=0)                         # refused: the second chunk fails whole
    with pytest.raises(FakeUiRefused) as err:
        batch.submit()
    assert str(err.value) == "ui.canvas: unknown op"   # the refusal itself is unchanged
    assert batch.sent == 128
    assert "128 of 130 ops were applied" in " ".join(err.value.__notes__)
    assert canvas.get().num_elements == 128


def test_a_routing_error_does_not_stop_game_events_or_ui_polling(conn, fake, monkeypatch):
    """F6: a bad ui event is logged; the ring pump and the ui poll carry on."""
    from botwithus.api import ui as ui_api
    ticks = []
    conn.events.subscribe(ServerTick, ticks.append)
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    real = ui_api.UiEvent.from_native
    calls = {"n": 0}

    def flaky(raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("bad event")
        return real(raw)

    monkeypatch.setattr(ui_api.UiEvent, "from_native", staticmethod(flaky))
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    fake.push(fake.hosts[0], root.handle, "destroyed", "removed")
    assert wait_until(lambda: calls["n"] >= 1)
    layer = ctx.ui.layer(ctx.ui.component(1477, 62))
    fake.push(fake.hosts[0], layer.handle, "destroyed", "removed")
    assert wait_until(lambda: got)                  # the ui thread survived the bad event
    fake.hosts[0].events.append(_tick_record(5))
    assert wait_until(lambda: ticks)


def test_ui_polling_ends_with_the_last_ui_script_and_restarts_on_next_use(conn, fake):
    """F7."""
    ctx = _ctx(conn)
    ctx.ui.component(*GAME_AREA)
    assert wait_until(lambda: conn._ui_events.running and fake.polls > 0)
    ctx._release_ui()
    assert wait_until(lambda: not conn._ui_events.running)
    polls = fake.polls
    time.sleep(0.15)
    assert fake.polls == polls                      # no poll with no UI script left
    _ctx(conn).ui.component(*GAME_AREA)
    assert wait_until(lambda: conn._ui_events.running and fake.polls > polls)


# --- review round 2 -------------------------------------------------------------------------

def test_a_shared_jagex_handle_claimed_after_its_destroyed_is_dead_and_gets_the_event(conn, fake):
    """L1 (the reviewer's router-level reproduction): A holds Jagex handle H; destroyed for H
    is routed; then B's lookup returns the same H. B must not hold a dead handle."""
    a, b = _ctx(conn, "a"), _ctx(conn, "b")
    got_b = []
    b.events.subscribe(UiEvent, got_b.append)
    root_a = a.ui.component(*GAME_AREA)
    b.ui.component(1477, 62)                       # B is a live UI script before the event
    router = conn._ui_router
    assert router.route([{"handle": root_a.handle, "hook": "destroyed", "reason": "removed"}]) == 1
    root_b = b.ui.component(*GAME_AREA)            # the agent answers with the same handle
    assert root_b.handle == root_a.handle
    assert not root_b.alive and not root_a.alive
    router.route([])
    assert [(e.handle, e.reason) for e in got_b] == [(root_a.handle, "removed")]


def test_a_ui_call_after_release_is_refused_and_sends_nothing(conn, fake):
    """L2: a component made after release would be tracked and freed by no one."""
    from botwithus.api.ui import STOPPED
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    ctx._release_ui()
    sent = len(fake.calls)
    for call in (lambda: ctx.ui.layer(root), lambda: ctx.ui.component(*GAME_AREA),
                 lambda: ctx.ui.handles()):
        with pytest.raises(RuntimeError) as err:
            call()
        assert str(err.value) == STOPPED
    assert len(fake.calls) == sent


def test_a_create_in_flight_across_release_is_freed_at_once(conn, fake):
    """L2: release runs while a create is in flight; the new component must not outlive it."""
    from botwithus.api.ui import STOPPED
    ctx = _ctx(conn)
    root = ctx.ui.component(*GAME_AREA)
    fake.block_verb = "create"
    outcome = []

    def make():
        try:
            outcome.append(ctx.ui.layer(root))
        except RuntimeError as exc:
            outcome.append(exc)

    maker = threading.Thread(target=make, daemon=True)
    maker.start()
    assert fake.blocked.wait(EVENT_WAIT_S)
    ctx._release_ui()                               # nothing owned yet: sends nothing
    fake.unblock.set()
    maker.join(EVENT_WAIT_S)
    (exc,) = outcome
    assert isinstance(exc, RuntimeError) and str(exc) == STOPPED
    (create,) = [c for c in fake.verbs("create")]
    destroys = [c[1] for c in fake.verbs("destroy")]
    assert len(destroys) == 1                       # the new component, freed straight away
    alive = fake.agent_components(fake.hosts[0])
    assert destroys[0] not in alive
    assert [h for h, i in alive.items() if not i["jagex"]] == []


def test_one_malformed_event_does_not_drop_the_rest_of_the_batch(conn, fake):
    """L3."""
    ctx = _ctx(conn)
    got = []
    ctx.events.subscribe(UiEvent, got.append)
    layer = ctx.ui.layer(ctx.ui.component(*GAME_AREA))
    delivered = conn._ui_router.route([
        {"handle": "not-a-handle", "hook": "destroyed"},
        {"handle": layer.handle, "hook": "destroyed", "reason": "removed"},
    ])
    assert delivered == 1
    assert [(e.handle, e.reason) for e in got] == [(layer.handle, "removed")]
    assert not layer.alive
