"""GameApi.schedule_break: seconds in the API, milliseconds on the wire.

The agent's ``schedule_break`` reads ``duration`` as milliseconds and refuses anything
above INT32_MAX. These tests pin the exact value sent, so a lost ``* 1000`` (a "300 s"
break lasting 0.3 s) or a doubled one fails here rather than in a live session.
"""
import math

import pytest

from botwithus.api.game import MAX_BREAK_MS, MAX_BREAK_SECONDS, GameApi, break_duration_ms
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient

from fakes import FakeNative

INT32_MAX = 2**31 - 1


class _RecordingRpc:
    """Records every call and answers with the agent's empty-map reply."""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        return {}


def _sent(duration_seconds):
    rpc = _RecordingRpc()
    GameApi(rpc, snapshot=lambda: None).schedule_break(duration_seconds)
    assert len(rpc.calls) == 1
    return rpc.calls[0]


def test_five_minutes_goes_out_as_300000_ms():
    method, params = _sent(300)
    assert (method, params) == ("schedule_break", {"duration": 300000})
    assert type(params["duration"]) is int


@pytest.mark.parametrize("seconds, ms", [
    (1, 1000),
    (0.001, 1),           # smallest accepted
    (0.0015, 2),          # nearest ms (1.5 -> 2, round half to even)
    (2.5, 2500),
    (MAX_BREAK_SECONDS, INT32_MAX),   # the agent's cap, exactly
])
def test_converted_to_whole_milliseconds(seconds, ms):
    method, params = _sent(seconds)
    assert method == "schedule_break"
    assert params == {"duration": ms}
    assert type(params["duration"]) is int


def test_cap_matches_the_agent():
    assert MAX_BREAK_MS == INT32_MAX


@pytest.mark.parametrize("seconds", [
    0, 0.0, -1, -300, 0.0004,          # rounds below 1 ms: the agent would no-op silently
    MAX_BREAK_SECONDS + 0.001,         # one ms over the agent's cap
    2147484,                           # whole seconds just over the cap
    math.inf, -math.inf, math.nan,
])
def test_out_of_range_is_refused_before_sending(seconds):
    rpc = _RecordingRpc()
    with pytest.raises(ValueError, match="duration_seconds"):
        GameApi(rpc, snapshot=lambda: None).schedule_break(seconds)
    assert rpc.calls == []


@pytest.mark.parametrize("seconds", [True, "300", None])
def test_non_numbers_are_refused(seconds):
    with pytest.raises(TypeError, match="duration_seconds"):
        break_duration_ms(seconds)


def test_interrupt_break_sends_no_params():
    rpc = _RecordingRpc()
    GameApi(rpc, snapshot=lambda: None).interrupt_break()
    assert rpc.calls == [("interrupt_break", None)]


def test_wire_request_carries_milliseconds():
    """Through the real RpcClient: the MsgPack request the host would send holds 300000."""
    seen = []
    native_mod.set_backend(FakeNative(responder=lambda m, p: seen.append((m, p)) or {}))
    try:
        GameApi(RpcClient(NativeHost.attach(4242)), snapshot=lambda: None).schedule_break(300)
    finally:
        native_mod.set_backend(None)
    assert seen == [("schedule_break", {"duration": 300000})]
    # 300000.0 == 300000, so pin the type: MsgPack would put a float on the wire.
    assert type(seen[0][1]["duration"]) is int
