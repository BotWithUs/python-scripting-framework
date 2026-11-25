"""MessagePack round-trip, event decode, and snapshot decode -- no client, no native host.

The event ring's seq drain now lives in the native host; here the fake native hands back
already-drained ``{"type", "body"}`` records and the suite pins the Python body decode.
"""
import logging
import struct

from botwithus.api import events as E
from botwithus.core import layout as L
from botwithus.core import msgpack_codec
from botwithus.core import native as native_mod
from botwithus.core.events import EventRingReader
from botwithus.core.native import NativeHost
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_events, build_snapshot


def _reader(events) -> EventRingReader:
    native_mod.set_backend(FakeNative(events=events))
    return EventRingReader(NativeHost.attach(4242))


def test_msgpack_roundtrip():
    obj = {"method": "queue_action", "id": 7,
           "params": {"action_id": 23, "param1": 1, "param2": 3200, "param3": 3200}}
    assert msgpack_codec.unpack(msgpack_codec.pack(obj)) == obj


def test_event_decode():
    tick_body = struct.pack("<i", 42)
    login_body = struct.pack("<ii", 10, 30)
    chat_body = struct.pack("<iHH", 0, 3, 2) + b"Bob" + b"hi"
    reader = _reader(build_events([
        (L.EVT_TICK, tick_body),
        (L.EVT_LOGIN_STATE_CHANGE, login_body),
        (L.EVT_CHAT_MESSAGE, chat_body),
    ]))
    evts = reader.poll()
    assert [type(e).__name__ for e in evts] == ["ServerTick", "LoginStateChanged", "ChatMessage"]
    assert evts[0].tick == 42
    assert (evts[1].old_state, evts[1].new_state) == (10, 30)
    assert (evts[2].sender, evts[2].text) == ("Bob", "hi")


# ActionRefusedBody, packed by hand from Events.h (NXTLibrary 026c86c), not with the decoder's
# own format: actionId 23, param1 -1, param2 3200, param3 0x01020304, reason 2
# (anchor_timeout), anchorCase 1 (scroll), heldMs 0xFFFFFFFE (uint32: must not read negative).
ACTION_REFUSED_BODY = bytes.fromhex(
    "17000000"      # +0  int32  actionId   23
    "ffffffff"      # +4  int32  param1     -1
    "800c0000"      # +8  int32  param2     3200
    "04030201"      # +12 int32  param3     0x01020304
    "02000000"      # +16 uint32 reason     2
    "01000000"      # +20 uint32 anchorCase 1
    "feffffff"      # +24 uint32 heldMs     4294967294
)


def test_action_refused_decodes_a_hand_packed_body():
    assert len(ACTION_REFUSED_BODY) == 28
    assert L.EVT_ACTION_REFUSED == 41
    (evt,) = _reader(build_events([(L.EVT_ACTION_REFUSED, ACTION_REFUSED_BODY)])).poll()
    assert evt == E.ActionRefused(0, 23, -1, 3200, 0x01020304, E.RefuseReason.ANCHOR_TIMEOUT,
                                  E.AnchorCase.SCROLL, 0xFFFFFFFE)
    assert evt.reason is E.RefuseReason.ANCHOR_TIMEOUT
    assert evt.anchor_case is E.AnchorCase.SCROLL


def test_action_refused_enums_are_the_agents_values():
    assert {m.name: m.value for m in E.RefuseReason} == {
        "NO_TARGET": 1, "ANCHOR_TIMEOUT": 2, "NO_OPENER": 3}
    assert {m.name: m.value for m in E.AnchorCase} == {
        "NONE": 0, "SCROLL": 1, "OPEN": 2, "CAMERA": 3, "WAIT": 4}


def test_action_refused_keeps_an_unknown_reason_as_its_int():
    body = ACTION_REFUSED_BODY[:16] + struct.pack("<III", 9, 7, 0)
    (evt,) = _reader(build_events([(L.EVT_ACTION_REFUSED, body)])).poll()
    assert (evt.reason, evt.anchor_case, evt.held_ms) == (9, 7, 0)
    assert type(evt.reason) is int and type(evt.anchor_case) is int


def test_action_refused_with_a_truncated_body_is_skipped(caplog):
    reader = _reader(build_events([
        (L.EVT_ACTION_REFUSED, ACTION_REFUSED_BODY[:24]),
        (L.EVT_TICK, struct.pack("<i", 5)),
    ]))
    with caplog.at_level(logging.DEBUG, logger="botwithus.events"):
        assert [type(e).__name__ for e in reader.poll()] == ["ServerTick"]
    assert any("ActionRefused" in r.getMessage() and "24 bytes" in r.getMessage()
               for r in caplog.records)


def test_action_refused_reaches_its_bus_subscribers():
    bus = E.EventBus()
    got = []
    bus.subscribe(E.ActionRefused, got.append)
    bus.subscribe(E.ActionExecuted, lambda e: got.append("executed"))
    for evt in _reader(build_events([(L.EVT_ACTION_REFUSED, ACTION_REFUSED_BODY)])).poll():
        bus.publish(evt)
    assert len(got) == 1 and got[0].action_id == 23


# KeyInputBody, packed by hand from Events.h (NXTLibrary ee221d2, the merge of PR #84), not with the
# decoder's own format: key VK_SHIFT (16), isAlt 0, isCtrl 1, isShift 1, then the flags byte.
def _key_body(flags: int, key: bytes = b"\x10\x00\x00\x00",
              mods: bytes = b"\x00\x01\x01") -> bytes:
    return key + mods + bytes([flags])


def _one_key(body: bytes) -> E.KeyInput:
    (evt,) = _reader(build_events([(L.EVT_KEY_INPUT, body)])).poll()
    return evt


def _flags(evt: E.KeyInput) -> tuple[bool, bool, bool]:
    return (evt.synthetic, evt.text_input_focused, evt.is_repeat)


def test_key_input_with_no_flags_is_what_an_older_agent_sends():
    assert L.EVT_KEY_INPUT == 30
    evt = _one_key(_key_body(0x00))
    assert evt == E.KeyInput(0, 16, False, True, True, False, False, False)


def test_key_input_each_flag_bit_maps_to_one_field():
    assert L.KEY_FLAG_SYNTHETIC == 1
    assert L.KEY_FLAG_TEXT_INPUT_FOCUSED == 2
    assert L.KEY_FLAG_REPEAT == 4
    assert _flags(_one_key(_key_body(0x01))) == (True, False, False)
    assert _flags(_one_key(_key_body(0x02))) == (False, True, False)
    assert _flags(_one_key(_key_body(0x04))) == (False, False, True)
    assert _flags(_one_key(_key_body(0x07))) == (True, True, True)


def test_key_input_reserved_flag_bits_are_ignored():
    evt = _one_key(_key_body(0xF8))
    assert evt is not None and _flags(evt) == (False, False, False)
    assert _flags(_one_key(_key_body(0xFD))) == (True, False, True)


def test_key_input_modifiers_and_key():
    evt = _one_key(_key_body(0, key=b"\x41\x00\x00\x00", mods=b"\x01\x00\x00"))
    assert (evt.key, evt.is_alt, evt.is_ctrl, evt.is_shift) == (0x41, True, False, False)
    evt = _one_key(_key_body(0, key=b"\x12\x00\x00\x00", mods=b"\x01\x01\x01"))
    assert (evt.key, evt.is_alt, evt.is_ctrl, evt.is_shift) == (18, True, True, True)
    evt = _one_key(_key_body(0, key=b"\xfe\xff\xff\xff"))   # uint32: must not read negative
    assert evt.key == 0xFFFFFFFE


def test_key_input_ignores_bytes_past_the_eighth():
    evt = _one_key(_key_body(0x02) + b"\xff\xff\xff\xff")
    assert evt == E.KeyInput(0, 16, False, True, True, False, True, False)


def test_key_input_with_a_short_body_is_skipped(caplog):
    reader = _reader(build_events([
        (L.EVT_KEY_INPUT, _key_body(0x07)[:7]),
        (L.EVT_KEY_INPUT, b""),
        (L.EVT_TICK, struct.pack("<i", 5)),
    ]))
    with caplog.at_level(logging.DEBUG, logger="botwithus.events"):
        assert [type(e).__name__ for e in reader.poll()] == ["ServerTick"]
    assert any("KeyInput" in r.getMessage() and "7 bytes" in r.getMessage()
               for r in caplog.records)


def test_key_input_positional_construction_still_works():
    evt = E.KeyInput(3, 65, False, False, True)
    assert _flags(evt) == (False, False, False)


def test_second_poll_is_empty():
    reader = _reader(build_events([(L.EVT_TICK, struct.pack("<i", 1))]))
    assert len(reader.poll()) == 1  # one drain returns the record...
    assert reader.poll() == []      # ...and the native ring cursor is now empty


def test_unknown_event_type_is_skipped():
    reader = _reader(build_events([
        (L.EVT_TICK, struct.pack("<i", 5)),
        (999, b"\x00\x00\x00\x00"),
        (L.EVT_BREAK_ENDED, b""),
    ]))
    evts = reader.poll()
    assert [type(e).__name__ for e in evts] == ["ServerTick", "BreakEnded"]


def test_snapshot_decode():
    buf = build_snapshot(server_tick=1234, game_cycle=9000, publish_seq=8999,
                         self_tile=(3100, 3400, 1), npc=(55, 1234, (3101, 3401)))
    snap = SnapshotView(buf)
    assert snap.in_game
    assert snap.server_tick == 1234
    assert snap.game_cycle == 9000
    assert snap.publish_seq == 8999  # distinct number space from game_cycle
    me = snap.self_player
    assert me.valid
    assert (me.tile_x, me.tile_y, me.plane) == (3100, 3400, 1)
    assert me.combat_level == 126
    assert len(me.skills) == 1 and me.skills[0].actual_level == 99
    npcs = snap.npcs()
    assert len(npcs) == 1
    assert npcs[0].server_index == 55 and npcs[0].type_id == 1234
    assert npcs[0].tile.x == 3101


def test_snapshot_counts_are_clamped_to_caps():
    buf = bytearray(build_snapshot())
    struct.pack_into("<I", buf, L.SNAP_NPC_COUNT, 999999)  # torn/huge count
    snap = SnapshotView(bytes(buf))
    assert len(snap.npcs()) == L.CAP_NPCS  # clamped, never overruns
