"""Input dialog: the exact key packing on the wire, and input refused before it is sent.

The packed vectors are the agent's (NXTLibrary Actions.h ComponentTrigger, scenario
iface-input-bank-x): 1469:4 is param1 96272388, a top-level key trigger is param2 720895,
(-1, '3') is param3 -65485 and Enter (84, 0) is 5505024. The Lua suite pins the same vectors.
"""
import pytest

from botwithus.api import input as I
from botwithus.api.actions import GameAction
from botwithus.api.game import GameApi
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient, RpcError

from fakes import FakeNative

P1 = 96272388   # (1469 << 16) | 4
P2 = 720895     # (10 << 16) | 0xFFFF
ENTER = 5505024
BACKSPACE = 5570560
ESCAPE = 851968


class FakeDialog:
    """An agent that answers the varc reads and records every queue_actions batch."""

    def __init__(self, mode: int = I.InputMode.AMOUNT, text: str = "", accept: int | None = None):
        self.mode = mode
        self.text = text
        self.accept = accept
        self.batches: list[list[dict]] = []

    def __call__(self, method, params):
        if method == "get_varc_int":
            return {"value": self.mode if params["var_id"] == I.VARC_INPUT_MODE else -1}
        if method == "get_varc_string":
            return {"value": self.text if params["var_id"] == I.VARC_INPUT_TEXT else ""}
        if method == "queue_actions":
            self.batches.append(params["actions"])
            n = len(params["actions"])
            return {"queued": n if self.accept is None else self.accept}
        return "__error__:unexpected " + method

    def arg0s(self) -> list[int]:
        assert len(self.batches) == 1, "every input call must be one batch"
        batch = self.batches[0]
        assert all(a["action_id"] == 5003 and a["param1"] == P1 and a["param2"] == P2
                   for a in batch)
        return [a["param3"] for a in batch]


def _game(agent: FakeDialog) -> GameApi:
    native_mod.set_backend(FakeNative(responder=agent))
    return GameApi(RpcClient(NativeHost.attach(4242)), lambda: None)


def _dialog(agent: FakeDialog) -> I.InputDialog:
    return I.InputDialog(_game(agent))


def _chars(text: str) -> list[int]:
    return [-65536 + ord(c) for c in text]


# --- packing -------------------------------------------------------------------------

def test_keystroke_packs_the_agent_layout():
    assert I.KeyStroke.character("3").packed() == -65485
    assert I.ENTER.packed() == ENTER
    assert I.BACKSPACE.packed() == BACKSPACE
    assert I.ESCAPE.packed() == ESCAPE
    assert I.KeyStroke(18).packed() == 18 << 16
    assert I.key_action(1469, 4, I.ENTER) == GameAction(5003, P1, P2, ENTER)


@pytest.mark.parametrize("args", [(0x8000, 0), (-0x8001, 0), (-1, -1), (-1, 0x10000), (True, 0)])
def test_keystroke_refuses_values_that_do_not_fit(args):
    with pytest.raises(ValueError):
        I.KeyStroke(*args)


@pytest.mark.parametrize("c", ["", "ab", "\t", "\x7f", "é"])
def test_character_takes_one_printable_ascii_char(c):
    with pytest.raises(I.InputRejected):
        I.KeyStroke.character(c)


def test_component_trigger_legacy_rule_for_key_triggers():
    # Zero high half: a bare Jagex key code, so 84 is Enter, not 'T'.
    assert I.component_trigger(1469, 4, 10, 84).param3 == ENTER
    # Non-zero high half: already packed, passed through.
    assert I.component_trigger(1469, 4, 10, -65485).param3 == -65485
    assert I.component_trigger(1469, 4, 10, ENTER).param3 == ENTER
    assert I.component_trigger(1469, 4, 10, 0).param3 == 0
    # Other trigger types never get the rule.
    assert I.component_trigger(1469, 4, 0, 84) == GameAction(5003, P1, 0xFFFF, 84)


def test_component_trigger_keeps_a_sub_slot_and_high_ids_in_int32():
    a = I.component_trigger(0xFFFF, 0xFFFF, 10, 18, sub=5)
    assert a.param1 == -1
    assert a.param2 == (10 << 16) | 5
    assert a.param3 == 18 << 16


@pytest.mark.parametrize("kwargs", [
    {"iface": -1}, {"iface": 0x10000}, {"comp": 0x10000}, {"sub": 0x8000}, {"trigger_type": -1},
])
def test_component_trigger_refuses_values_that_do_not_fit(kwargs):
    args = {"iface": 1469, "comp": 4, "trigger_type": 10, **kwargs}
    with pytest.raises(ValueError):
        I.component_trigger(**args)


# --- raw key calls ---------------------------------------------------------------------

def test_fire_keys_sends_one_batch_and_returns_the_count():
    agent = FakeDialog()
    assert I.fire_keys(_game(agent), 1469, 4, [I.KeyStroke(18), I.KeyStroke.character("3")]) == 2
    assert agent.arg0s() == [18 << 16, -65485]


def test_fire_keys_refuses_more_than_one_batch():
    agent = FakeDialog()
    with pytest.raises(ValueError):
        I.fire_keys(_game(agent), 1469, 4, [I.ENTER] * (I.MAX_BATCH + 1))
    assert agent.batches == []


def test_type_text_types_without_submitting():
    agent = FakeDialog()
    assert I.type_text(_game(agent), 1469, 4, "12") == 2
    assert agent.arg0s() == _chars("12")


# --- the dialog ------------------------------------------------------------------------

def test_enter_amount_types_each_digit_then_enter_in_one_batch():
    agent = FakeDialog()
    assert _dialog(agent).enter_amount(3) is True
    assert agent.arg0s() == [-65485, ENTER]


def test_enter_amount_with_k_suffix():
    agent = FakeDialog()
    assert _dialog(agent).enter_amount("10k")
    assert agent.arg0s() == [-65487, -65488, -65429, ENTER]


def test_enter_text_types_a_name():
    agent = FakeDialog(mode=I.InputMode.NAME)
    assert _dialog(agent).enter_text("Bob_1")
    assert agent.arg0s() == [-65470, -65425, -65438, -65441, -65487, ENTER]


def test_submit_cancel_backspace_and_clear():
    for call, expected in [
        (lambda d: d.submit(), [ENTER]),
        (lambda d: d.cancel(), [ESCAPE]),
        (lambda d: d.backspace(2), [BACKSPACE, BACKSPACE]),
        (lambda d: d.clear(), [BACKSPACE] * 3),
    ]:
        agent = FakeDialog(text="12k")
        assert call(_dialog(agent)) is True
        assert agent.arg0s() == expected


def test_clear_of_an_empty_dialog_sends_nothing():
    agent = FakeDialog(text="")
    assert _dialog(agent).clear() is True
    assert agent.batches == []


def test_mode_and_text():
    assert _dialog(FakeDialog(mode=7)).mode() is I.InputMode.AMOUNT
    assert _dialog(FakeDialog(mode=2)).mode() is I.InputMode.NAME
    assert _dialog(FakeDialog(mode=0)).mode() is I.InputMode.CLOSED
    assert _dialog(FakeDialog(mode=9)).mode() is I.InputMode.OTHER
    assert not _dialog(FakeDialog(mode=0)).is_open()
    assert _dialog(FakeDialog(mode=9)).is_open()
    assert _dialog(FakeDialog(mode=7, text="42")).text() == "42"
    assert _dialog(FakeDialog(mode=0, text="42")).text() == ""  # varc 2506 outlives the dialog


def test_unset_mode_is_closed():
    # Live: in game, before the dialog has ever been opened, varc 5 reads -1.
    agent = FakeDialog(mode=-1, text="42")
    d = _dialog(agent)
    assert d.mode() is I.InputMode.CLOSED
    assert not d.is_open()
    assert d.text() == ""
    assert d.submit() is False
    assert d.enter_amount(3) is False
    assert agent.batches == []


def test_a_failed_mode_read_raises():
    native_mod.set_backend(FakeNative(responder=lambda m, p: "__error__:rpc timeout"))
    d = I.InputDialog(GameApi(RpcClient(NativeHost.attach(4242)), lambda: None))
    with pytest.raises(RpcError):
        d.submit()


# --- refused, nothing sent ---------------------------------------------------------------

@pytest.mark.parametrize("amount", [
    "b", "1.5", "1 0", "k", "k5", "5kk", "5k3", "5b", "5MM", "M5", "", "12345678901",
    -1, True, 1.0, None, 2_147_483_648, "2147483648", "2147484k", "2148m",
])
def test_bad_amounts_raise_before_sending(amount):
    agent = FakeDialog()
    with pytest.raises(I.InputRejected):
        _dialog(agent).enter_amount(amount)
    assert agent.batches == []


@pytest.mark.parametrize("amount", ["2147483647", "2147483k", "2147m", "2147M", "5K", "5M", "0"])
def test_amount_edges_that_are_accepted(amount):
    agent = FakeDialog()
    assert _dialog(agent).enter_amount(amount)
    assert agent.arg0s() == _chars(amount) + [ENTER]


@pytest.mark.parametrize("name", ["", "thirteenchars", "bob@home", "José", "a\tb", None])
def test_bad_names_raise_before_sending(name):
    agent = FakeDialog(mode=I.InputMode.NAME)
    with pytest.raises(I.InputRejected):
        _dialog(agent).enter_text(name)
    assert agent.batches == []


def test_bad_argument_raises_even_when_the_dialog_is_closed():
    agent = FakeDialog(mode=I.InputMode.CLOSED)
    with pytest.raises(I.InputRejected):
        _dialog(agent).enter_amount("b")


@pytest.mark.parametrize("mode, typed, call", [
    (I.InputMode.AMOUNT, "123456789", lambda d: d.enter_amount(10)),   # 11 chars in total
    (I.InputMode.AMOUNT, "5k", lambda d: d.enter_amount(3)),           # a digit after the suffix
    (I.InputMode.AMOUNT, "2147483", lambda d: d.enter_amount("648")),  # over 2^31-1 in total
    (I.InputMode.NAME, "abcdefghij", lambda d: d.enter_text("xyz")),   # 13 chars in total
])
def test_text_already_typed_that_would_break_the_rules_returns_false(mode, typed, call):
    agent = FakeDialog(mode=mode, text=typed)
    assert call(_dialog(agent)) is False
    assert agent.batches == []


@pytest.mark.parametrize("mode, call", [
    (I.InputMode.CLOSED, lambda d: d.enter_amount(3)),
    (I.InputMode.NAME, lambda d: d.enter_amount(3)),
    (I.InputMode.AMOUNT, lambda d: d.enter_text("bob")),
    (9, lambda d: d.enter_text("bob")),
    (I.InputMode.CLOSED, lambda d: d.submit()),
    (I.InputMode.CLOSED, lambda d: d.cancel()),
    (I.InputMode.CLOSED, lambda d: d.backspace()),
    (I.InputMode.CLOSED, lambda d: d.clear()),
])
def test_wrong_or_closed_dialog_returns_false(mode, call):
    agent = FakeDialog(mode=mode, text="12")
    assert call(_dialog(agent)) is False
    assert agent.batches == []


def test_a_partly_queued_batch_raises():
    agent = FakeDialog(accept=1)
    with pytest.raises(I.InputDialogError, match="queued 1 of 2"):
        _dialog(agent).enter_amount(3)
