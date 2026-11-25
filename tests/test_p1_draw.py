"""Debug draw over ``debug_draw_*`` and ``highlight_*``.

The wire maps asserted here are the parameter names the agent's ``MatchDrawScalar`` /
``MatchDrawParam`` table reads (NXTLibrary src/rpc/Handlers.cpp): x1..y2, x/y/w/h, text, label,
points, iface/comp, npc/player/self, plane, color, thickness, filled, closed, z, ttl_ms, font.
A key the table does not list is ignored by the agent, so a misspelt key would draw with a
default silently: these tests fail on any key that differs.
"""
import pytest

from botwithus.api import draw
from botwithus.api.draw import DrawBatchResult, DrawEntry
from botwithus.core.rpc import MalformedReply, RpcError

from fake_agent_reads import RecordingAgent, game_with

STYLE = draw.Style(color=0x80112233, thickness=3, filled=True, z=-7, ttl_ms=1500)
STYLE_WIRE = {"color": 0x80112233, "thickness": 3, "filled": True, "closed": False, "z": -7,
              "ttl_ms": 1500}


def _echo_key(params):
    return {"key": params.get("key", "auto")}


def test_rect_wire_map_is_exact():
    agent = RecordingAgent({"debug_draw_set": _echo_key})
    key = game_with(agent).draw.set(draw.rect("box", 101, 202, 33, 44, style=STYLE))
    assert key == "box"
    assert agent.calls == [("debug_draw_set", {"key": "box", "kind": "rect", "space": "screen",
                                               "x": 101, "y": 202, "w": 33, "h": 44,
                                               **STYLE_WIRE})]


def test_default_style_always_sends_a_ttl():
    assert draw.line("l", 1, 2, 3, 4).wire() == {
        "key": "l", "kind": "line", "space": "screen", "x1": 1, "y1": 2, "x2": 3, "y2": 4,
        "color": draw.GREEN, "thickness": 1, "filled": False, "closed": False, "z": 0,
        "ttl_ms": draw.DEFAULT_TTL_MS}


def test_world_primitive_takes_subtiles_and_plane():
    cmd = draw.ellipse("e", 3200 * 256, 3201 * 256, 256, 512, world=True, plane=2)
    wire = cmd.wire()
    assert (wire["space"], wire["plane"], wire["x"], wire["y"]) == ("world", 2, 819_200, 819_456)


def test_text_poly_and_component_box_wire():
    assert draw.text("t", 5, 6, "hello", font="heading").wire()["text"] == "hello"
    assert draw.text("t", 5, 6, "hello", font="heading").wire()["font"] == "heading"
    poly = draw.poly("p", [(1, 2), (3, 4), (5, 6)], style=draw.Style(closed=True)).wire()
    assert poly["points"] == [1, 2, 3, 4, 5, 6] and poly["closed"] is True
    box = draw.component_box("c", 1473, 5, label="pack").wire()
    assert (box["kind"], box["iface"], box["comp"], box["label"]) == ("component", 1473, 5, "pack")
    assert "text" not in box  # the agent refuses "text" on a component


@pytest.mark.parametrize("build", [
    lambda: draw.rect("k" * 48, 0, 0, 1, 1),                      # 48 UTF-8 bytes
    lambda: draw.rect("é" * 24, 0, 0, 1, 1),                      # 48 UTF-8 bytes, 24 chars
    lambda: draw.rect("", 0, 0, 1, 1),
    lambda: draw.rect("r", 0, 0, 0, 1),                           # w must be positive
    lambda: draw.rect("r", 0, 0, 1, 1, plane=1),                  # plane on screen space
    lambda: draw.rect("r", 0, 0, 1, 1, world=True, plane=4),
    lambda: draw.poly("p", [(1, 2)]),                             # one point
    lambda: draw.poly("p", [(i, i) for i in range(33)]),          # 33 points
    lambda: draw.text("t", 0, 0, ""),
    lambda: draw.text("t", 0, 0, "x" * 128),                      # 128 UTF-16 units
    lambda: draw.text("t", 0, 0, "x", font="bold"),
    lambda: draw.Style(thickness=65),
    lambda: draw.Style(z=0x8000),
    lambda: draw.Style(color=-1),
    lambda: draw.Style(ttl_ms=-1),
    lambda: draw.highlight_tile(3200, 3200, plane=4),
    lambda: draw.highlight_area(3200, 3200, 0, 2),
    lambda: draw.highlight_npc(-1),
])
def test_what_the_agent_would_refuse_raises_at_the_call_site(build):
    with pytest.raises(ValueError):
        build()


def test_47_byte_key_is_accepted():
    assert draw.rect("é" * 23 + "x", 0, 0, 1, 1).key.encode("utf-8").__len__() == 47


def test_set_batch_sends_items_and_reports_dropped():
    # Failure: a batch that drew nothing read as success, or "count" mistaken for "dropped".
    agent = RecordingAgent({"debug_draw_set_batch": {"count": 1, "dropped": 2,
                                                     "error": "thickness must be 1..64"}})
    cmds = [draw.line("a", 0, 0, 9, 9), draw.rect("b", 1, 1, 2, 2), draw.text("c", 3, 3, "x")]
    got = game_with(agent).draw.set_batch(cmds)
    assert got == DrawBatchResult(applied=1, dropped=2, error="thickness must be 1..64")
    assert agent.calls == [("debug_draw_set_batch", {"items": [c.wire() for c in cmds]})]


def test_set_batch_nil_error_is_none():
    agent = RecordingAgent({"debug_draw_set_batch": {"count": 2, "dropped": 0, "error": None}})
    got = game_with(agent).draw.set_batch([draw.line("a", 0, 0, 1, 1)] * 2)
    assert got.error is None and got.applied == 2


def test_set_batch_refuses_over_cap_and_highlights_unsent():
    agent = RecordingAgent({"debug_draw_set_batch": {"count": 0, "dropped": 0, "error": None}})
    game = game_with(agent)
    with pytest.raises(ValueError):
        game.draw.set_batch([draw.line("a", 0, 0, 1, 1)] * 257)
    with pytest.raises(TypeError):
        game.draw.set_batch([draw.highlight_tile(3200, 3200)])
    assert agent.calls == []


def test_highlight_npc_rides_highlight_entity_with_label_and_auto_key():
    agent = RecordingAgent({"highlight_entity": {"key": "npc:42"}})
    key = game_with(agent).draw.highlight(draw.highlight_npc(42, w=2, h=3, label="boss",
                                                             style=STYLE))
    assert key == "npc:42"
    assert agent.calls == [("highlight_entity", {"npc": 42, "w": 2, "h": 3, "label": "boss",
                                                 **STYLE_WIRE})]


def test_highlight_variants_route_to_their_methods():
    agent = RecordingAgent({m: _echo_key for m in (
        "highlight_entity", "highlight_tile", "highlight_area", "highlight_component")})
    d = game_with(agent).draw
    d.highlight(draw.highlight_self(key="me"))
    d.highlight(draw.highlight_player(7))
    d.highlight(draw.highlight_tile(3222, 3218, 1))
    d.highlight(draw.highlight_area(3200, 3200, 5, 4, 2, key="zone"))
    d.highlight(draw.highlight_component(1473, 5))
    got = [(m, {k: v for k, v in p.items() if k not in STYLE_WIRE}) for m, p in agent.calls]
    assert got == [
        ("highlight_entity", {"self": True, "w": 1, "h": 1, "key": "me"}),
        ("highlight_entity", {"player": 7, "w": 1, "h": 1}),
        ("highlight_tile", {"x": 3222, "y": 3218, "plane": 1}),
        ("highlight_area", {"x": 3200, "y": 3200, "plane": 2, "w": 5, "h": 4, "key": "zone"}),
        ("highlight_component", {"iface": 1473, "comp": 5}),
    ]


def test_set_refuses_a_highlight_and_highlight_refuses_a_primitive():
    d = game_with(RecordingAgent()).draw
    with pytest.raises(TypeError):
        d.set(draw.highlight_tile(1, 1))
    with pytest.raises(TypeError):
        d.highlight(draw.rect("r", 0, 0, 1, 1))


def test_clear_and_clear_all():
    agent = RecordingAgent({"debug_draw_clear": {"removed": 2},
                            "debug_draw_clear_all": {"removed": 9, "scope": "mine"}})
    d = game_with(agent).draw
    assert d.clear("a", "b", "missing") == 2
    assert d.clear_all() == 9
    assert agent.calls == [("debug_draw_clear", {"keys": ["a", "b", "missing"]}),
                           ("debug_draw_clear_all", {"scope": "mine"})]
    with pytest.raises(ValueError):
        d.clear()


def _row(i):
    return {"key": f"k{i}", "kind": "rect", "space": "world", "plane": 1,
            "project": "on_screen", "color": 0xFF00FF00 + i, "thickness": 2, "z": i,
            "filled": True, "closed": False, "geom": [i, i + 1, i + 2, i + 3],
            "resolved": True, "rect": [10, 20, 30, 40], "ttl_ms": 1234, "font": "small",
            "text": ""}


def test_list_pages_until_total_and_decodes_every_field():
    # Failure: stopping after the first page of 128, or a field read from a neighbour key.
    rows = [_row(i) for i in range(130)]

    def page(params):
        off, lim = params["offset"], params["limit"]
        chunk = rows[off:off + lim]
        return {"total": 130, "offset": off, "returned": len(chunk), "items": chunk}

    agent = RecordingAgent({"debug_draw_list": page})
    got = game_with(agent).draw.list()
    assert len(got) == 130
    assert agent.calls == [("debug_draw_list", {"offset": 0, "limit": 128}),
                           ("debug_draw_list", {"offset": 128, "limit": 128})]
    assert got[129] == DrawEntry("k129", "rect", "world", 1, "on_screen", 0xFF00FF00 + 129, 2,
                                 129, True, False, (129, 130, 131, 132), True,
                                 (10, 20, 30, 40), 1234, "small", "")


def test_list_malformed_row():
    agent = RecordingAgent({"debug_draw_list": {"total": 1, "items": [{"key": "x"}]}})
    with pytest.raises(MalformedReply):
        game_with(agent).draw.list()


def test_enable_reads_and_sets():
    agent = RecordingAgent({"debug_draw_enable": lambda p: {"enabled": p.get("enabled", True)}})
    d = game_with(agent).draw
    assert d.enabled() is True
    assert d.set_enabled(False) is False
    assert agent.calls == [("debug_draw_enable", {}), ("debug_draw_enable", {"enabled": False})]


def test_stats_typed_fields_and_raw():
    reply = {"enabled": True, "running": True, "count": 17, "capacity": 512, "dropped": 3,
             "has_presented": False, "present_failures": 4, "backend": "layered",
             "frames": 88}
    stats = game_with(RecordingAgent({"debug_draw_stats": reply})).draw.stats()
    assert (stats.count, stats.capacity, stats.dropped, stats.present_failures) == (17, 512, 3, 4)
    assert stats.has_presented is False and stats.backend == "layered"
    assert stats.raw["frames"] == 88


def test_set_refusal_is_rpc_error():
    agent = RecordingAgent({"debug_draw_set": "__error__:draw store is full"})
    with pytest.raises(RpcError):
        game_with(agent).draw.set(draw.rect("r", 0, 0, 1, 1))


def test_argb():
    assert draw.argb(0x11, 0x22, 0x33) == 0xFF112233
    assert draw.argb(1, 2, 3, 4) == 0x04010203
    with pytest.raises(ValueError):
        draw.argb(256, 0, 0)
