"""J3 QuestLog, pause/resume (runner and waits), writable settings, logout / exit_to_lobby."""
import json
import threading
import time
from pathlib import Path

from botwithus.api import BotScript
from botwithus.api.cache_values import CacheValues
from botwithus.api.quests import (MembershipRequired, PriorQuestRequired, QuestLog, QuestStage,
                                  SkillRequired)
from botwithus.api.script import ScriptContext
from botwithus.api.settings import ScriptSettings
from botwithus.core.runtime import ScriptRunner

from fake_agent_reads import RecordingAgent, game_with
from fake_game import FakeGame, FakeSnapshot, me, skill

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "j2_cache.json").read_text())
QUESTS = {int(k): v for k, v in FIXTURES["quest"].items()}
# The live cache's quest ids are dense (0..531 in the 2026-06 dump, no gaps): pad the real
# records with filler so the stub scan sees the same shape.
for _filler in range(0, 300):
    QUESTS.setdefault(_filler, {"id": _filler, "name": f"Filler {_filler}",
                                "listName": f"Filler {_filler}", "progressVarps": [],
                                "progressVarbits": []})


def _log(game, quests=None):
    return QuestLog(game, CacheValues.from_tables(quests=quests or QUESTS))


# --- quests -----------------------------------------------------------------------------

def test_started_is_at_or_above_the_start_value():
    # Cook's Assistant 257: varp 2492, start 1, end 2. Failure: v1's "above start" rule, under
    # which no value of the variable ever reads started.
    game = FakeGame()
    log = _log(game)
    for value, stage in ((0, QuestStage.NOT_STARTED), (1, QuestStage.STARTED),
                         (2, QuestStage.COMPLETE), (7, QuestStage.COMPLETE)):
        game.varp_values[2492] = value
        status = log.status(257)
        assert (status.stage, status.progress) == (stage, value)
    assert log.is_started(257) and log.is_complete(257)


def test_a_stub_record_reads_its_listed_twin():
    # Failure: stub 6 / 132 reported UNKNOWN (no progress variable) instead of reading 257 / 258.
    game = FakeGame()
    game.varp_values[2492] = 1
    game.varbit_values[9655] = 3
    log = _log(game)
    cook = log.status(6)
    assert (cook.quest_id, cook.name, cook.stage) == (257, "Cook's Assistant", QuestStage.STARTED)
    vamp = log.status(132)
    assert (vamp.quest_id, vamp.stage, vamp.variable.id) == (258, QuestStage.COMPLETE, 9655)


def test_an_unreadable_variable_or_missing_quest_is_unknown_never_guessed():
    game = FakeGame()
    log = _log(game)
    assert log.status(257).stage is QuestStage.UNKNOWN       # varp unavailable
    assert log.status(9999).stage is QuestStage.UNKNOWN
    assert not log.is_started(9999) and log.progress(9999) == -1


def test_unmet_requirements():
    # Tower of Life (1): members, Construction 10. Failure: a met requirement listed, or a
    # missing one dropped.
    quests = dict(QUESTS)
    quests[500] = dict(QUESTS[1], id=500, name="After", listName="After",
                       dependentQuestIds=[257], membersOnly=False, skillRequirements=[])
    game = FakeGame(FakeSnapshot(self_player=me(skills=[skill(22, 9, 1)], member=False)))
    game.varp_values[2492] = 1
    log = _log(game, quests)
    assert log.unmet_requirements(1) == [MembershipRequired(), SkillRequired(22, 10, 9)]
    assert log.unmet_requirements(500) == [PriorQuestRequired(257)]
    game.varp_values[2492] = 2
    game.snap.self_player = me(skills=[skill(22, 10, 1)], member=True)
    assert log.requirements_met(1) and log.requirements_met(500)


# --- pause / resume ---------------------------------------------------------------------

class _Looper(BotScript):
    def __init__(self):
        self.loops = 0

    def on_loop(self):
        self.loops += 1
        return 10


def test_a_paused_runner_calls_no_on_loop_until_resumed_and_a_stop_still_ends_it():
    # Failure: on_loop still called while paused, or a paused script that ignores the stop.
    stop = threading.Event()
    ctx = ScriptContext("p", FakeGame(), None, stop)
    script = _Looper()
    runner = ScriptRunner(script, ctx)
    runner.start()
    time.sleep(0.1)
    ctx.pause()
    time.sleep(0.05)
    held = script.loops
    time.sleep(0.25)
    assert ctx.is_paused and script.loops in (held, held + 1)
    ctx.resume()
    time.sleep(0.1)
    assert script.loops > held + 1
    ctx.pause()
    runner.stop(join_timeout_s=2)
    assert not runner.thread_alive


def test_ctx_waits_hold_while_paused():
    stop = threading.Event()
    ctx = ScriptContext("w", FakeGame(), None, stop)
    ctx.pause()
    threading.Timer(0.3, ctx.resume).start()
    start = time.monotonic()
    assert ctx.delay_until(lambda: True, 50) is True     # held, then the condition is met
    assert time.monotonic() - start >= 0.25


# --- settings ---------------------------------------------------------------------------

def test_settings_merge_ui_values_over_script_keys():
    # Failure: a UI apply replacing the script's own keys instead of merging.
    ctx = ScriptContext("s", FakeGame(), None, threading.Event())
    ctx._set_config({"mode": "fast", "count": 3})
    ctx.settings.put("last_run", 1700)
    ctx.settings.put_bool("seen_intro", True)
    ctx._set_config({"mode": "slow", "count": 3})
    assert ctx.settings.as_dict() == {"mode": "slow", "count": "3", "last_run": "1700",
                                      "seen_intro": "true"}
    assert ctx.settings.get_int("count") == 3 and ctx.settings.get_bool("seen_intro")
    assert ctx.settings.get_int("mode", 9) == 9
    assert ctx.settings.remove("last_run") and not ctx.settings.remove("last_run")
    assert ctx.settings.save() is False          # no settings store on the native host
    assert dict(ctx.config) == {"mode": "slow", "count": 3}


def test_settings_are_typed_strings():
    s = ScriptSettings({"a": False})
    assert s.get("a") == "false" and s.get_string("missing", "x") == "x"


# --- logout / exit to lobby -------------------------------------------------------------

def test_logout_and_exit_to_lobby_call_the_agents_rpcs():
    # Failure: a different method name than the agent's handler table (Handlers.cpp).
    agent = RecordingAgent({"logout": {}, "exit_to_lobby": {}})
    game = game_with(agent)
    assert game.logout() is True and game.exit_to_lobby() is True
    assert agent.methods() == ["logout", "exit_to_lobby"]
