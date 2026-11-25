"""The agent's session plan (``get_humanizer_session``) and the pacing that steps back for it.

Replies are written in the wire's own keys, as the agent's session RPC sends them. Every time in
them is absolute on the client clock; the checks pin that each one surfaces as a delta against
the reply's own ``now_ms``. Each test says what result would have counted as a failure.
"""
import pytest

from botwithus.api import PlannedBreak, SessionPacing, paced_delay
from botwithus.api.pacing import (CARRIER_HELD_ACTION, CARRIER_NEXT_ACTION, INACTIVE,
                                  MIN_DEFERRED_DELAY_MS)
from botwithus.core.rpc import MalformedReply

from fake_agent_reads import RecordingAgent, game_with

NOW = 5_000_000
PRESS_IN = 840
BREAK_IN = 300
BREAK_LEN = 45_000
NEXT_BREAK = 61_000
INTO_BREAK = 1_000
SCRIPT_DELAY = 1_200


def _reply(**over):
    reply = {"active": True, "mode": "active", "now_ms": NOW, "held": False, "press_ms": 0,
             "break_ms": 0, "break_start_ms": 0, "break_end_ms": 0, "next_break_ms": 0,
             "persona_seed": 1234567890123, "counters": {"planned": 1}}
    reply.update(over)
    return reply


def _held_with_break(start):
    return _reply(held=True, break_ms=BREAK_LEN, break_start_ms=start,
                  break_end_ms=start + BREAK_LEN, press_ms=start + BREAK_LEN + PRESS_IN)


def test_inactive_reply_is_inactive():
    # Failure: an agent with the layer off read as owning the timing.
    assert SessionPacing.from_reply({"active": False, "mode": "absent"}) is INACTIVE


def test_held_press_is_a_delta_on_the_client_clock():
    # Failure: the absolute press time leaked through, or the delta taken against a host clock.
    pacing = SessionPacing.from_reply(_reply(held=True, press_ms=NOW + PRESS_IN))
    assert (pacing.active, pacing.held, pacing.next_press_in_ms) == (True, True, PRESS_IN)
    assert pacing.planned_break is None


def test_an_overdue_press_is_zero_not_negative():
    assert SessionPacing.from_reply(_reply(held=True, press_ms=NOW - 5)).next_press_in_ms == 0


def test_break_ahead_inside_the_hold():
    # Failure: the break missing, or its start/length read from the wrong key.
    pacing = SessionPacing.from_reply(_held_with_break(NOW + BREAK_IN))
    assert pacing.planned_break == PlannedBreak(CARRIER_HELD_ACTION, BREAK_IN, BREAK_LEN)
    assert pacing.next_press_in_ms == BREAK_IN + BREAK_LEN + PRESS_IN


def test_break_under_way_reports_what_is_left():
    brk = SessionPacing.from_reply(_held_with_break(NOW - INTO_BREAK)).planned_break
    assert (brk.starts_in_ms, brk.length_ms) == (0, BREAK_LEN - INTO_BREAK)


def test_break_over_is_not_reported():
    assert SessionPacing.from_reply(_held_with_break(NOW - 2 * BREAK_LEN)).planned_break is None


def test_next_action_break_is_visible_before_dispatch():
    # Failure: a script could not see the break it is about to walk into.
    pacing = SessionPacing.from_reply(_reply(next_break_ms=NEXT_BREAK))
    assert pacing.planned_break == PlannedBreak(CARRIER_NEXT_ACTION, 0, NEXT_BREAK)


def test_unreadable_clock_keeps_only_the_break_length():
    pacing = SessionPacing.from_reply({**_held_with_break(NOW + BREAK_IN), "now_ms": 0})
    assert pacing.active and pacing.next_press_in_ms == 0
    assert pacing.planned_break == PlannedBreak(CARRIER_HELD_ACTION, 0, BREAK_LEN)


def test_paced_delay_steps_back_while_the_agent_owns_timing():
    # Failure: the script's own delay stacked on the agent's planned gap.
    held = SessionPacing(True, True, PRESS_IN, None)
    assert paced_delay(held, SCRIPT_DELAY) == PRESS_IN
    assert paced_delay(SessionPacing(True), SCRIPT_DELAY) == MIN_DEFERRED_DELAY_MS
    assert paced_delay(INACTIVE, SCRIPT_DELAY) == SCRIPT_DELAY


def test_game_session_pacing_reads_the_agent():
    agent = RecordingAgent({"get_humanizer_session": _reply(held=True, press_ms=NOW + PRESS_IN)})
    game = game_with(agent)
    assert game.session_pacing().next_press_in_ms == PRESS_IN
    assert game.paced_delay(SCRIPT_DELAY) == PRESS_IN
    assert agent.methods() == ["get_humanizer_session", "get_humanizer_session"]


def test_an_agent_predating_the_method_is_asked_once():
    # Failure: an older agent paid a round trip on every loop, or read as anything but inactive.
    agent = RecordingAgent({"get_humanizer_session":
                            "__error__:method not found: get_humanizer_session"})
    game = game_with(agent)
    for _ in range(3):
        assert game.session_pacing() is INACTIVE
        assert game.paced_delay(SCRIPT_DELAY) == SCRIPT_DELAY
    assert agent.methods() == ["get_humanizer_session"]


def test_another_refusal_is_inactive_for_that_call_only():
    agent = RecordingAgent({"get_humanizer_session": "__error__:not_in_game"})
    game = game_with(agent)
    assert game.session_pacing() is INACTIVE
    assert game.session_pacing() is INACTIVE
    assert agent.methods() == ["get_humanizer_session", "get_humanizer_session"]


def test_an_active_reply_without_its_timing_is_malformed():
    # Failure: "active" with no press time read as "press now".
    with pytest.raises(MalformedReply):
        game_with(RecordingAgent({"get_humanizer_session": {"active": True}})).session_pacing()
