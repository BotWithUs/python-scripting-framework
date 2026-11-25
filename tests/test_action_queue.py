"""queue_action / queue_actions against the agent's all-or-nothing contract (NXTLibrary #67).

The agent queues a whole batch and replies ``{queued: len}``, or refuses the whole batch with
``queue_actions: <code>`` and queues none of it. The native host passes the refusal through
``rpc_raw`` verbatim, says ``rpc timeout`` for a call with no reply, and a dead pipe is a
transport failure. The strings here are the agent's and the host's, verbatim.
"""
import pytest

from botwithus.api import input as I
from botwithus.api.actions import GameAction
from botwithus.api.game import (
    MAX_ACTION_BATCH,
    ActionOutcomeUnknown,
    ActionsRefused,
    GameApi,
)
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import MalformedReply, RpcClient, RpcError

from fakes import FakeNative

WALK = GameAction(23, 1, 3200, 3200)


class FakeQueue:
    """An agent whose queue_action(s) answer is set per test."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return self.reply(method, params) if callable(self.reply) else self.reply


def _game(agent: FakeQueue) -> GameApi:
    native_mod.set_backend(FakeNative(responder=agent))
    return GameApi(_rpc(), lambda: None)


def _rpc() -> RpcClient:
    return RpcClient(NativeHost.attach(4242))


def test_a_whole_batch_returns_its_length():
    agent = FakeQueue(lambda m, p: {"queued": len(p["actions"])})
    assert _game(agent).queue_actions([WALK, WALK, WALK]) == 3
    assert agent.calls[0][1]["actions"][0] == {
        "action_id": 23, "param1": 1, "param2": 3200, "param3": 3200}


@pytest.mark.parametrize("code", [
    "queue_full", "batch_too_large", "action_id_required", "bad_param actions"])
def test_a_refused_batch_raises_actions_refused_with_the_code(code):
    game = _game(FakeQueue(f"__error__:queue_actions: {code}"))
    with pytest.raises(ActionsRefused) as info:
        game.queue_actions([WALK])
    assert info.value.code == code
    assert info.value.method == "queue_actions"
    assert isinstance(info.value, RpcError)   # one except RpcError still covers it


def test_a_refused_single_action_raises_actions_refused():
    game = _game(FakeQueue("__error__:queue_action: queue_full"))
    with pytest.raises(ActionsRefused) as info:
        game.queue_action(WALK)
    assert info.value.code == "queue_full"


@pytest.mark.parametrize("text", ["rpc timeout", "rpc_timeout", "rpc protocol error"])
def test_a_timeout_or_unreadable_reply_is_outcome_unknown_not_refused(text):
    """The agent may have queued already: no reply in time, or one the host could not read."""
    game = _game(FakeQueue(f"__error__:{text}"))
    with pytest.raises(ActionOutcomeUnknown) as info:
        game.queue_actions([WALK])
    assert not isinstance(info.value, ActionsRefused)
    with pytest.raises(ActionOutcomeUnknown):
        game.queue_action(WALK)


def test_a_reply_too_large_to_encode_is_never_reported_as_refused():
    """Raised after the agent answered: whatever it is, it is not 'nothing queued'."""
    game = _game(FakeQueue("__error__:rpc_raw: response too large to encode"))
    with pytest.raises(RpcError) as info:
        game.queue_actions([WALK])
    assert not isinstance(info.value, ActionsRefused)


def test_a_dead_pipe_is_a_connection_error():
    game = _game(FakeQueue("__error__:pipe disconnected"))
    with pytest.raises(ConnectionError):
        game.queue_actions([WALK])


def test_another_methods_error_text_is_not_mistaken_for_a_refusal():
    game = _game(FakeQueue("__error__:something else went wrong"))
    with pytest.raises(RpcError) as info:
        game.queue_actions([WALK])
    assert type(info.value) is RpcError


def test_a_prefix_from_an_agent_older_than_67_is_returned():
    agent = FakeQueue({"queued": 1})
    assert _game(agent).queue_actions([WALK, WALK]) == 1


@pytest.mark.parametrize("reply", [{}, {"queued": 3}, {"queued": -1}, {"queued": True},
                                   {"queued": "2"}, [], None])
def test_an_impossible_queued_count_is_a_malformed_reply(reply):
    game = _game(FakeQueue(reply))
    with pytest.raises(MalformedReply):
        game.queue_actions([WALK, WALK])


def test_max_batch_is_the_agents_cap():
    assert MAX_ACTION_BATCH == 128
    assert I.MAX_BATCH == MAX_ACTION_BATCH


def test_input_dialog_surfaces_a_refusal_with_nothing_typed():
    def agent(method, params):
        if method == "get_varc_int":
            return {"value": I.InputMode.AMOUNT}
        if method == "get_varc_string":
            return {"value": ""}
        return "__error__:queue_actions: queue_full"
    native_mod.set_backend(FakeNative(responder=agent))
    dialog = I.InputDialog(GameApi(_rpc(), lambda: None))
    with pytest.raises(ActionsRefused, match="queue_full"):
        dialog.enter_amount(3)
