"""The client reads of NXTLibrary 0f746b4 (PUBLIC_PROTOCOL.md 6.2.1, 6.2.2, 6.3.1, 6.7, 6.8):
GE offers, the world list, set_world / hop_world, target mode and use-on, world_to_screen,
children with sub ids, and execute_script string args.

Replies are written in the wire's own keys, copied from the agent's handlers (NXTLibrary
``src/rpc/GameReadHandlers.cpp`` and ``src/rpc/Handlers.cpp`` at 0f746b4). Every value is
distinct and non-default, so a field read from the wrong key, or a default, reads as a wrong
value. Each test says what result would have counted as a failure.
"""
import pytest

from botwithus.api import game as game_mod
from botwithus.api.client_reads import ComponentRef, Child
from botwithus.api.snapshot_types import Tile
from botwithus.core.rpc import MalformedReply, RpcError

from fake_agent_reads import RecordingAgent, game_with

BACKPACK_PACKED = (1473 << 16) | 5   # 96534533, PUBLIC_PROTOCOL.md 6.3.1


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(game_mod, "USE_ON_POLL_S", 0.001)
    monkeypatch.setattr(game_mod, "USE_ON_ARM_TIMEOUT_S", 0.2)


def _offer(slot, stage=0, stage_name="empty", type_=0, type_name="buy", item=0, count=0,
           sold=0, price=0, gold=0):
    return {"slot": slot, "stage": stage, "stageName": stage_name, "type": type_,
            "typeName": type_name, "itemId": item, "itemCount": count, "soldCount": sold,
            "price": price, "goldFromSale": gold}


_GE = {"market": 0, "offers": [_offer(s) for s in range(8)]}
_GE["offers"][2] = _offer(2, 2, "added", 1, "sell", 1511, 500, 120, 5_000_000_000, 600_000_000_000)
_GE["offers"][5] = _offer(5, 5, "finished", 0, "buy", 946, 1, 1, 25, 25)


# --- get_ge_offers --------------------------------------------------------------------------

def test_ge_offers_sends_market_0_and_decodes_every_field():
    # Failure: market not sent as 0, a 64-bit price cut, or a slot's values under another slot.
    agent = RecordingAgent({"get_ge_offers": _GE})
    offers = game_with(agent).ge_offers()
    assert agent.calls == [("get_ge_offers", {"market": 0})]
    assert len(offers) == 8
    sell = offers[2]
    assert (sell.slot, sell.stage, sell.stage_name, sell.type_name) == (2, 2, "added", "sell")
    assert (sell.item_id, sell.item_count, sell.sold_count) == (1511, 500, 120)
    assert (sell.price, sell.gold_from_sale) == (5_000_000_000, 600_000_000_000)
    assert sell.is_sell and sell.in_use
    assert [o.slot for o in offers if o.in_use] == [2, 5]


def test_ge_offers_missing_key_is_malformed():
    # Failure: a default 0 read for the missing price.
    broken = {"market": 0, "offers": [{k: v for k, v in _offer(0).items() if k != "price"}]}
    with pytest.raises(MalformedReply):
        game_with(RecordingAgent({"get_ge_offers": broken})).ge_offers()


# --- get_world_list -------------------------------------------------------------------------

_WORLDS = {"count": 3, "total": 4, "skipped": 1, "truncated": False, "worlds": [
    {"id": 1, "flags": 0x89, "activity": "Trade - Members", "locationId": 2,
     "locationName": "United Kingdom", "population": 1234, "ping": 0},
    {"id": 2, "flags": 0x400000, "activity": "Classic", "locationId": 0,
     "locationName": "United States (east)", "population": 777, "ping": 0},
    {"id": 84, "flags": 0x800001, "activity": "", "locationId": 1, "locationName": "Germany",
     "population": 55, "ping": 0},
]}


def test_world_list_decodes_rows_and_the_four_flag_bits():
    # Failure: a flag bit read from the wrong mask, or info fields swapped.
    wl = game_with(RecordingAgent({"get_world_list": _WORLDS})).world_list()
    assert wl.is_fetched and (wl.total, wl.skipped, wl.truncated) == (4, 1, False)
    w1, w2, w84 = wl.worlds
    assert (w1.members, w1.skill_total, w1.classic_only, w1.eoc_only) == (True, True, False, False)
    assert (w2.members, w2.classic_only) == (False, True)
    assert (w84.members, w84.eoc_only) == (True, True)
    assert w1.flags == 0x89   # the unresolved 0x8 stays in the raw flags
    assert (w1.activity, w1.location_id, w1.location_name, w1.population) == \
        ("Trade - Members", 2, "United Kingdom", 1234)
    assert wl.get(84) is w84 and wl.get(3) is None


def test_an_empty_world_list_is_not_an_error():
    # Failure: a raise, or is_fetched True.
    empty = {"count": 0, "total": 0, "skipped": 0, "truncated": False, "worlds": []}
    wl = game_with(RecordingAgent({"get_world_list": empty})).world_list()
    assert not wl.is_fetched and wl.worlds == ()


def test_world_list_count_disagreeing_with_rows_is_malformed():
    bad = dict(_WORLDS, count=2)
    with pytest.raises(MalformedReply):
        game_with(RecordingAgent({"get_world_list": bad})).world_list()


# --- set_world / hop_world ------------------------------------------------------------------

class HopAgent(RecordingAgent):
    """set_world starts a hop; the world changes on the 3rd read and the state is 37 until the
    5th. World 84 starts but never lands; 999 is not listed."""

    def __init__(self):
        super().__init__({})
        self.world, self.target, self.reads = 1, None, 0

    def __call__(self, method, params):
        self.calls.append((method, dict(params or {})))
        if method == "set_world":
            wid = params["world_id"]
            if wid == 999:
                return {"started": False, "reason": "unknown_world"}
            self.target, self.reads = wid, 0
            return {"started": True}
        if method == "get_current_world":
            self.reads += 1
            if self.target not in (None, 84) and self.reads >= 3:
                self.world = self.target
            return {"world_id": self.world}
        if method == "get_login_state":
            moving = self.target not in (None, 84) and 3 <= self.reads < 5
            return {"state": 37 if moving else 30, "login_progress": 0, "login_status": 0}
        return super().__call__(method, params)


def test_hop_world_waits_for_the_world_and_state_30():
    # Failure: arrived at the first read of world 2 while the state is still 37 (3rd read).
    agent = HopAgent()
    r = game_with(agent).hop_world(2, timeout_s=5, poll_s=0.001)
    assert r.arrived and r.started and (r.world_id, r.game_state) == (2, 30)
    assert agent.calls[0] == ("set_world", {"world_id": 2})
    assert agent.reads == 5


def test_hop_world_refusal_returns_the_reason_without_polling():
    # Failure: a raise, arrived True, or any read after the refusal.
    agent = HopAgent()
    r = game_with(agent).hop_world(999, timeout_s=5, poll_s=0.001)
    assert (r.arrived, r.started, r.reason) == (False, False, "unknown_world")
    assert agent.methods() == ["set_world"]


def test_hop_world_times_out_on_a_world_that_never_lands():
    # Failure: arrived True, or a wait past the timeout by more than a poll.
    r = game_with(HopAgent()).hop_world(84, timeout_s=0.05, poll_s=0.005)
    assert not r.arrived and r.started and r.world_id == 1 and 0.05 <= r.elapsed_s < 1


def test_set_world_refuses_a_bad_id_before_sending():
    agent = HopAgent()
    with pytest.raises(ValueError):
        game_with(agent).set_world(0)
    assert agent.calls == []


# --- target mode and use-on -----------------------------------------------------------------

class TargetAgent(RecordingAgent):
    """get_target_mode armed by queue_action 25 on the backpack (not slot 27), cleared by
    58 / 8 / 2. A 25 elsewhere does not land."""

    def __init__(self):
        super().__init__({})
        self.armed_sub = None

    def __call__(self, method, params):
        if method == "queue_action":
            aid, p2, p3 = params["action_id"], params["param2"], params["param3"]
            if aid == 25 and p3 == BACKPACK_PACKED:
                self.armed_sub = None if p2 == 27 else p2
            elif aid in (58, 8, 2):
                self.armed_sub = None
        if method == "get_target_mode":
            self.calls.append((method, {}))
            return self._mode()
        return super().__call__(method, params)

    def _mode(self):
        if self.armed_sub is None:
            return {"active": False, "isItem": False, "objId": -1, "source": None, "verb": "",
                    "name": "", "targetMask": 0}
        return {"active": True, "isItem": True, "objId": 946,
                "source": {"iface": 1473, "comp": 5, "sub": self.armed_sub}, "verb": "Use",
                "name": "<col=ff9040>Knife", "targetMask": 31}


def test_target_mode_decodes_armed_and_idle():
    # Failure: a source read while idle, or the armed sub / item / text from the wrong key.
    agent = TargetAgent()
    game = game_with(agent)
    idle = game.target_mode()
    assert (idle.active, idle.obj_id, idle.source, idle.verb) == (False, -1, None, "")
    agent.armed_sub = 4
    m = game.target_mode()
    assert (m.active, m.is_item, m.obj_id, m.target_mask) == (True, True, 946, 31)
    assert m.source == ComponentRef(1473, 5, 4)
    assert (m.verb, m.name) == ("Use", "<col=ff9040>Knife")


def test_use_item_on_item_arms_with_25_then_fires_58():
    # Failure: arming with 57, the sub / packed id in the wrong param, or no clear afterwards.
    agent = TargetAgent()
    game = game_with(agent)
    assert game.use_item_on_item(0, 3) is True
    assert agent.queued() == [(25, 0, 0, BACKPACK_PACKED), (58, 0, 3, BACKPACK_PACKED)]
    assert game.target_mode().active is False


def test_use_on_npc_and_object_pack_their_targets():
    agent = TargetAgent()
    game = game_with(agent)
    assert game.use_item_on_npc(1, 30) and game.use_item_on_object(2, 1278, 3200, 3201)
    assert agent.queued() == [(25, 0, 1, BACKPACK_PACKED), (8, 30, 0, 0),
                              (25, 0, 2, BACKPACK_PACKED), (2, 1278, 3200, 3201)]


def test_use_on_never_fires_when_the_arm_does_not_land():
    # Failure: True, or a target action queued after an arm that never armed.
    agent = TargetAgent()
    assert game_with(agent).use_item_on_item(27, 3) is False
    assert agent.queued() == [(25, 0, 27, BACKPACK_PACKED)]


def test_use_on_does_not_take_an_earlier_arm_as_its_own():
    # Failure: True because target mode was already active from slot 1.
    agent = TargetAgent()
    agent.armed_sub = 1
    src = ComponentRef(1477, 25, -1)
    assert game_with(agent).use_on_npc(src, 30) is False
    assert agent.queued() == [(25, 0, -1, (1477 << 16) | 25)]


# --- world_to_screen ------------------------------------------------------------------------

def _w2s(params):
    out = []
    for p in params["points"]:
        if p.get("plane", 0) != 0:
            out.append({"result": "wrong_plane", "onScreen": False, "x": None, "y": None})
        elif (p["x"], p["y"]) == (3200, 3201):
            out.append({"result": "ok", "onScreen": True, "x": 641, "y": 359})
        else:
            out.append({"result": "behind_camera", "onScreen": False, "x": None, "y": None})
    return {"viewport": {"x": 8, "y": 30, "width": 1264, "height": 682}, "points": out}


def test_world_to_screen_sends_points_and_decodes_each():
    # Failure: a plane -1 sent for a tile without one (the fake answers wrong_plane), x/y
    # invented for a point without, or the viewport dropped.
    agent = RecordingAgent({"world_to_screen": _w2s})
    proj = game_with(agent).world_to_screen([(3200, 3201), Tile(2900, 3200, 0), (3200, 3201, 1)])
    assert agent.calls == [("world_to_screen", {"points": [
        {"x": 3200, "y": 3201}, {"x": 2900, "y": 3200, "plane": 0},
        {"x": 3200, "y": 3201, "plane": 1}]})]
    ok, behind, wrong = proj.points
    assert (ok.result, ok.on_screen, ok.x, ok.y) == ("ok", True, 641, 359)
    assert (behind.on_screen, behind.x, behind.y) == (False, None, None)
    assert wrong.result == "wrong_plane"
    assert (proj.viewport.x, proj.viewport.width, proj.viewport.height) == (8, 1264, 682)


def test_world_to_screen_refuses_0_or_65_tiles_before_sending():
    agent = RecordingAgent({"world_to_screen": _w2s})
    game = game_with(agent)
    with pytest.raises(ValueError):
        game.world_to_screen([])
    with pytest.raises(ValueError):
        game.world_to_screen([(3200, 3200)] * 65)
    assert agent.calls == []


# --- children with sub ----------------------------------------------------------------------

def test_dynamic_children_with_sub_sends_withsub_and_reads_pairs():
    # Failure: withSub not sent (the agent answers the flat list), or comp and sub swapped.
    agent = RecordingAgent({"get_dynamic_children": {"children": [
        {"comp": 5, "sub": 0}, {"comp": 5, "sub": 1}, {"comp": 5, "sub": 2}]}})
    kids = game_with(agent).dynamic_children_with_sub(1473, 5)
    assert agent.calls == [("get_dynamic_children", {"iface": 1473, "comp": 5, "withSub": True})]
    assert kids == (Child(5, 0), Child(5, 1), Child(5, 2))


def test_a_flat_children_list_is_malformed_in_the_sub_form():
    # Failure: an int read as a {comp, sub} pair, i.e. an agent before withSub misread.
    agent = RecordingAgent({"get_static_children": {"children": [6, 7]}})
    with pytest.raises(MalformedReply):
        game_with(agent).static_children_with_sub(1473, 5)


# --- execute_script string args -------------------------------------------------------------

def test_execute_script_sends_string_args_in_order():
    # Failure: string_args dropped (the pre-change wrapper never sent them), or reordered.
    agent = RecordingAgent({"execute_script": {"returns": [42, 1_000_000_002_009, ""]}})
    got = game_with(agent).execute_script(0x100000001, [7, 35], ["int", "long", "string"],
                                          string_args=["abc", "héllo"])
    assert agent.calls == [("execute_script", {
        "handle": 0x100000001, "int_args": [7, 35], "string_args": ["abc", "héllo"],
        "returns": ["int", "long", "string"]})]
    assert got == [42, 1_000_000_002_009, ""]


@pytest.mark.parametrize("ints,strs,returns", [
    ([1] * 10, ["x"] * 7, None),          # 17 args together
    (None, ["a" * 256], None),             # 256 bytes
    (None, ["é" * 128], None),        # 256 UTF-8 bytes in 128 characters
    (None, None, ["float"]),               # an unknown kind
])
def test_execute_script_refuses_what_the_agent_would_before_sending(ints, strs, returns):
    agent = RecordingAgent({"execute_script": {"returns": []}})
    with pytest.raises(ValueError):
        game_with(agent).execute_script(1, ints, returns, string_args=strs)
    assert agent.calls == []


def test_execute_script_255_bytes_is_allowed():
    agent = RecordingAgent({"execute_script": {"returns": []}})
    game_with(agent).execute_script(1, None, None, string_args=["a" * 255])
    assert agent.calls[0][1]["string_args"] == ["a" * 255]


def test_a_refused_read_raises_the_agents_code():
    agent = RecordingAgent({"get_target_mode": "__error__:not_available"})
    with pytest.raises(RpcError) as exc:
        game_with(agent).target_mode()
    assert "not_available" in str(exc.value)
