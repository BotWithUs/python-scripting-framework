"""Dialog: options read from one ``get_interface_tree`` reply, selection on the ROW.

The tree reply carries the agent's keys (iface, comp, category, text, parent: the BFS parent
index). The rows are laid out as the Java host's Dialog javadoc records for 1188, live: the
label lives three levels under row component 8, beside a "1." number cell.
"""
import pytest

from botwithus.api.dialog import Dialog, DialogOption, cells, normalize

from fake_agent_reads import RecordingAgent, game_with

DIALOGUE = 30
LAYER, TEXT, SPRITE = 1, 3, 4


def _node(comp, category, parent, text=""):
    return {"iface": 1188, "comp": comp, "sub": -1, "category": category, "text": text,
            "parent": parent}


# BFS order, as the agent writes it.
_TREE = {"count": 12, "nodes": [
    _node(0, LAYER, -1),                                  # 0 container
    _node(8, LAYER, 0),                                   # 1 row "1."
    _node(13, LAYER, 0),                                  # 2 row "2."
    _node(40, TEXT, 0, "Select an option"),               # 3 title: not a LAYER row
    _node(18, LAYER, 0),                                  # 4 empty row: no label
    _node(3, LAYER, 1),                                   # 5
    _node(10, TEXT, 2, "2."),                             # 6
    _node(11, LAYER, 2),                                  # 7
    _node(4, TEXT, 5, "1."),                              # 8
    _node(5, LAYER, 5),                                   # 9
    _node(12, TEXT, 7, "<col=ffffff>Goodbye.</col>"),     # 10
    _node(6, TEXT, 9, "What's   wrong?"),                 # 11
]}


def _dialog(agent):
    return Dialog(game_with(agent))


def test_options_pair_each_label_with_its_row_and_number():
    # Failure: the label leaf's comp (6) instead of the row's (8), the title read as an option,
    # or an empty row listed.
    agent = RecordingAgent({"get_interface_tree": _TREE})
    got = _dialog(agent).options()
    assert agent.calls == [("get_interface_tree", {"iface": 1188, "comp": 0})]
    assert got == [DialogOption(1, "What's   wrong?", 1188, 8),
                   DialogOption(2, "<col=ffffff>Goodbye.</col>", 1188, 13)]


def test_select_by_text_queues_dialogue_on_the_row():
    agent = RecordingAgent({"get_interface_tree": _TREE})
    assert _dialog(agent).select("goodbye") is True
    assert agent.queued() == [(DIALOGUE, 0, -1, (1188 << 16) | 13)]


def test_select_text_tolerates_case_markup_and_spacing():
    agent = RecordingAgent({"get_interface_tree": _TREE})
    assert _dialog(agent).select("WHAT'S WRONG") is True
    assert agent.queued() == [(DIALOGUE, 0, -1, (1188 << 16) | 8)]


def test_select_by_index():
    agent = RecordingAgent({"get_interface_tree": _TREE})
    assert _dialog(agent).select(2) is True
    assert agent.queued() == [(DIALOGUE, 0, -1, (1188 << 16) | 13)]


@pytest.mark.parametrize("choice", ["nope", 3, "", "   "])
def test_no_match_sends_nothing(choice):
    agent = RecordingAgent({"get_interface_tree": _TREE})
    assert _dialog(agent).select(choice) is False
    assert agent.queued() == []


def test_closed_dialog_has_no_options():
    agent = RecordingAgent({"get_interface_tree": {"count": 0, "nodes": []}})
    dialog = _dialog(agent)
    assert dialog.options() == [] and dialog.is_open() is False


def test_continue_chat_drives_1184_15_when_open():
    agent = RecordingAgent({"get_component": {"iface": 1184, "comp": 0}})
    assert _dialog(agent).continue_chat() is True
    assert agent.calls[0] == ("get_component", {"iface": 1184, "comp": 0})
    assert agent.queued() == [(DIALOGUE, 0, -1, (1184 << 16) | 15)]


def test_continue_chat_closed_sends_nothing():
    agent = RecordingAgent({"get_component": {"iface": -1, "comp": 0}})
    assert _dialog(agent).continue_chat() is False
    assert agent.queued() == []


def test_cells_keeps_a_number_shaped_label():
    # A quiz answer "4." after the "1." cell is the label, not a second index.
    assert cells(["1.", "4."], 9) == (1, "4.")
    assert cells(["Yes"], 3) == (3, "Yes")
    assert cells(["2."], 1) is None


def test_select_rejects_bool():
    with pytest.raises(TypeError):
        _dialog(RecordingAgent({"get_interface_tree": _TREE})).select(True)


def test_normalize():
    assert normalize("<col=ff0000>Hello</col>   World") == "hello world"
