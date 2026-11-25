"""The script-facing game API: a typed facade over the RPC catalog + live snapshot.

Reads that fit a polled snapshot go through ``snapshot()`` (zero round-trip). Reads
that don't, and every mutation, go over RPC. Method names here track the *live* agent
catalog (validated with ``rpc.list_methods``); scene/entity data is served from the
snapshot, not RPC, so there are deliberately no ``query_*`` calls here.

``GameApi`` is constructed by a ``Connection`` with an ``RpcClient``, a callable that
returns the current ``SnapshotView``, and a variable reader over the native host.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .actions import BACKPACK_COMPONENT, BACKPACK_IFACE, ActionType, GameAction
from .client_reads import (
    SCRIPT_ARGS_MAX,
    SCRIPT_RETURN_KINDS,
    SCRIPT_STRING_ARG_MAX,
    WORLD_TO_SCREEN_MAX,
    Child,
    ComponentRef,
    GeOffer,
    HopResult,
    Projection,
    SetWorldResult,
    TargetMode,
    WorldList,
    children_from_reply,
    ge_offers_from_reply,
    projection_from_reply,
    set_world_from_reply,
    target_mode_from_reply,
    world_list_from_reply,
)
from ..core import ctl
from .components import NO_SUB, ComponentOps, InterfaceComponent
from .draw import DebugDraw
from .inventory import (
    InventoryContainer,
    backpack_container,
    bank_container,
    equipment_container,
)
from .rpc_types import (
    ActionHistoryEntry,
    SlotObjVars,
    WorldSpotAnim,
    action_history_from_reply,
    obj_vars_from_reply,
    spot_anims_from_reply,
)
from .variables import VarbitRead, VarpRead, VarpState
from ..core.rpc import MalformedReply, RpcClient, RpcError
from ..core.snapshot import SnapshotView
from .snapshot_types import (
    ENTITY_REFUSALS,
    HeadbarList,
    HintArrowList,
    OverheadText,
    PlayerAppearance,
)

if TYPE_CHECKING:
    from .gameval import GamevalIndex
    from ..core.variables import VariableReader
    from .action_bar import ActionBar
    from .buffs import Buffs
    from .combat import LocalCombat
    from .component_query import ComponentQuery
    from .deposit_box import DepositBox, LootInventory
    from .hud import Hud
    from .lodestones import LodestoneNetwork
    from .prayers import Prayers
    from .quests import QuestLog
    from .summoning import Summoning

# The native host's texts for an action-queue call whose outcome it cannot know: no reply in
# time ("rpc timeout" from host_surface.cpp MapCall; "rpc_timeout" is the code
# bwu_queue_actions reports), or a reply it could not read ("rpc protocol error"). In each,
# the agent may already have queued the call.
_OUTCOME_UNKNOWN = frozenset({"rpc timeout", "rpc_timeout", "rpc protocol error"})


class ActionsRefused(RpcError):
    """The agent refused a ``queue_action`` / ``queue_actions`` call and queued **none** of it.

    ``code`` is the agent's reason without the method prefix: ``queue_full`` (the queue has
    no room for the whole batch), ``batch_too_large`` (more than :data:`MAX_ACTION_BATCH`),
    ``action_id_required`` (``queue_actions`` only: an action with id 0; a single
    ``queue_action`` with id 0 is a no-op instead), or ``bad_param <field>``. Nothing was
    queued, so the call can be retried as it is once the cause is gone.

    This is the **only** error that means nothing was queued. Any other error from
    ``queue_action`` / ``queue_actions`` leaves the outcome unknown."""

    def __init__(self, method: str, message: str) -> None:
        super().__init__(method, message)
        self.code = message.removeprefix(f"{method}: ")


class ActionOutcomeUnknown(RpcError):
    """A ``queue_action`` / ``queue_actions`` call timed out, or its reply could not be read:
    the agent may have queued all of it or none of it. Read :meth:`GameApi.action_queue_size`
    before you send it again. Every other error except :class:`ActionsRefused` is the same
    unknown: a lost connection (``ConnectionError``), :class:`MalformedReply`, or any other
    :class:`RpcError`."""


# The most actions one queue_actions call takes. The agent refuses a larger batch whole
# (``batch_too_large``); it never cuts one short.
MAX_ACTION_BATCH = 128

SnapshotProvider = Callable[[], SnapshotView]

# Local life points are NOT in the snapshot. The producer's LocalPlayer stops at
# the skills array and the native host hard-codes health to 0
# (native-scripting-host src/host_surface.cpp, ``self()``), so the only way to
# read them is these two varps over RPC. Ids mirrored from the Java host,
# JBotWithUsV2 core/.../impl/GameAPIImpl.java (VARP_CURRENT_HEALTH /
# VARP_MAX_HEALTH), which reads exactly this pair for the same reason.
VARP_CURRENT_HEALTH = 13537
VARP_MAX_HEALTH = 13538

# The agent keeps the last 128 dispatched actions (ActionQueue.cpp kHistoryCap) and caps
# get_action_history's max_results there.
MAX_ACTION_HISTORY = 128

MS_PER_SECOND = 1000
# The agent's ``schedule_break`` takes ``duration`` in milliseconds and refuses anything
# above INT32_MAX with ``bad_param duration`` (nxt-library src/rpc/Handlers.cpp,
# Handle_ScheduleBreak). A zero or negative duration is accepted there but schedules
# nothing, silently, so this side refuses it instead.
MAX_BREAK_MS = 2**31 - 1
MAX_BREAK_SECONDS = MAX_BREAK_MS / MS_PER_SECOND


def break_duration_ms(duration_seconds: float) -> int:
    """``duration_seconds`` as the whole milliseconds the agent's ``duration`` field takes.

    Rounds to the nearest millisecond. Raises ``TypeError`` for a non-number (``bool``
    included) and ``ValueError`` for a value that is not finite, rounds to under 1 ms, or
    exceeds :data:`MAX_BREAK_SECONDS` (about 24.8 days).
    """
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise TypeError(f"duration_seconds must be a number of seconds, "
                        f"not {type(duration_seconds).__name__}")
    if not math.isfinite(duration_seconds):
        raise ValueError(f"duration_seconds must be finite, got {duration_seconds!r}")
    ms = round(duration_seconds * MS_PER_SECOND)
    if ms < 1:
        raise ValueError(f"duration_seconds must be at least 0.001 s (1 ms), "
                         f"got {duration_seconds!r}")
    if ms > MAX_BREAK_MS:
        raise ValueError(f"duration_seconds must be at most {MAX_BREAK_SECONDS} s "
                         f"(the agent's limit is {MAX_BREAK_MS} ms), got {duration_seconds!r}")
    return ms


def _is_known(read: VarpRead) -> bool:
    """A value the game really holds: stored, or a default confirmed from the cache."""
    if read.state is VarpState.SET:
        return True
    return read.state is VarpState.DEFAULT_NOT_SET_CLIENTSIDE and read.default_verified


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_ENTITY_KINDS = ("npc", "player")

# How long the use-on helpers wait for the arm (SELECT_COMPONENT) to read active, and how often
# they look. The agent drains one action per 600 ms tick.
USE_ON_ARM_TIMEOUT_S = 3.0
USE_ON_POLL_S = 0.1


def _sleep_then_continue(seconds: float | None) -> bool:
    time.sleep(seconds or 0)
    return False   # "the awaited thing did not happen": wait_stop_aware keeps slicing


def _pause(seconds: float) -> bool:
    """Sleep ``seconds``; False when a GUI stop came first (always True under the CLI)."""
    return ctl.wait_stop_aware(_sleep_then_continue, seconds) is not None


def _is_armed_from(mode: TargetMode, source: ComponentRef) -> bool:
    return mode.active and mode.source == source


def _backpack(slot: int) -> ComponentRef:
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError(f"backpack slot must be an int >= 0, got {slot!r}")
    return ComponentRef(BACKPACK_IFACE, BACKPACK_COMPONENT, slot)


def _tile_point(tile: Any) -> dict[str, int]:
    """``{x, y[, plane]}`` from a Tile (``x``, ``y``, ``plane``) or an ``(x, y[, plane])``."""
    if hasattr(tile, "x") and hasattr(tile, "y"):
        x, y, plane = tile.x, tile.y, getattr(tile, "plane", None)
    else:
        x, y, *rest = tile
        plane = rest[0] if rest else None
    point = {"x": int(x), "y": int(y)}
    if plane is not None and plane >= 0:
        point["plane"] = int(plane)
    return point


def _script_params(handle: int, int_args: list[int] | None, string_args: list[str] | None,
                   returns: list[str] | None) -> dict[str, Any]:
    """``execute_script``'s params, refusing what the agent would refuse before sending it."""
    ints, strs, kinds = list(int_args or []), list(string_args or []), list(returns or [])
    if len(ints) + len(strs) > SCRIPT_ARGS_MAX:
        raise ValueError(f"execute_script takes at most {SCRIPT_ARGS_MAX} args together")
    for s in strs:
        if not isinstance(s, str) or len(s.encode("utf-8")) > SCRIPT_STRING_ARG_MAX:
            raise ValueError(f"string args must be str of at most {SCRIPT_STRING_ARG_MAX} "
                             f"UTF-8 bytes, got {s!r}")
    if any(k not in SCRIPT_RETURN_KINDS for k in kinds):
        raise ValueError(f"return kinds are {SCRIPT_RETURN_KINDS}, got {kinds!r}")
    params: dict[str, Any] = {"handle": handle}
    if ints:
        params["int_args"] = ints
    if strs:
        params["string_args"] = strs
    if kinds:
        params["returns"] = kinds
    return params


# What a by-gameval variable read returns when the name does not resolve (Java's
# GameAPI.UNRESOLVED_VARIABLE): the same -1 a missing or unreadable variable reads.
UNRESOLVED_VARIABLE = -1


class GameApi:
    def __init__(self, rpc: RpcClient, snapshot: SnapshotProvider,
                 variables: "VariableReader | None" = None) -> None:
        self._rpc = rpc
        self._snapshot = snapshot
        self._variables = variables
        self._draw = DebugDraw(rpc)
        # (server_tick, current, max) for the last KNOWN reading; see local_health.
        self._health: tuple[int, int, int] | None = None
        # The J1-J3 composition facades, made on first use and shared (each keeps its cache
        # reads), as the Java host's GameAPIImpl keeps one of each.
        self._facades: dict[str, Any] = {}

    def _facade(self, name: str, make: Callable[[], Any]) -> Any:
        found = self._facades.get(name)
        if found is None:
            found = self._facades.setdefault(name, make())
        return found

    # --- gameval names (native ABI 8; JBotWithUsV2 GameAPI.gamevals) ----------

    def gamevals(self) -> "GamevalIndex":
        """The gameval name index (:mod:`botwithus.api.gameval`): names to ids and back by type,
        from the local game cache. Never None; on a host without one every lookup is None."""
        from .gameval import default_index
        return default_index()

    def varp_by_gameval(self, gameval: str) -> int:
        """:meth:`varp` of the varp named ``gameval``; :data:`UNRESOLVED_VARIABLE` (-1) when the
        name does not resolve, as the Java host's ``getVarp(String)``."""
        found = self._gameval_id("VARP", gameval)
        return UNRESOLVED_VARIABLE if found is None else self.varp(found)

    def varp_long_by_gameval(self, gameval: str) -> int:
        found = self._gameval_id("VARP", gameval)
        return UNRESOLVED_VARIABLE if found is None else self.varp_long(found)

    def varbit_by_gameval(self, gameval: str) -> int:
        found = self._gameval_id("VARBIT", gameval)
        return UNRESOLVED_VARIABLE if found is None else self.varbit(found)

    def varc_int_by_gameval(self, gameval: str) -> int:
        found = self._gameval_id("VAR_CLIENT", gameval)
        return UNRESOLVED_VARIABLE if found is None else self.varc_int(found)

    def _gameval_id(self, type_name: str, gameval: str) -> int | None:
        from .gameval import GamevalType
        return self.gamevals().id(GamevalType[type_name], gameval)

    # --- composition APIs (JBotWithUsV2 phases J2/J3) ------------------------

    def action_bar(self) -> "ActionBar":
        """The combat action bar (:mod:`botwithus.api.action_bar`)."""
        from .action_bar import ActionBar
        return self._facade("action_bar", lambda: ActionBar(self))

    def prayers(self) -> "Prayers":
        """Prayer points, the active book, toggles and quick prayers."""
        from .prayers import Prayers
        return self._facade("prayers", lambda: Prayers(self))

    def combat(self) -> "LocalCombat":
        """Adrenaline, life/prayer/summoning points, own name, target, in combat."""
        from .combat import LocalCombat
        return self._facade("combat", lambda: LocalCombat(self, self.prayers()))

    def summoning(self) -> "Summoning":
        from .summoning import Summoning
        return self._facade("summoning", lambda: Summoning(self))

    def lodestones(self) -> "LodestoneNetwork":
        from .lodestones import LodestoneNetwork
        return self._facade("lodestones", lambda: LodestoneNetwork(self))

    def quests(self) -> "QuestLog":
        from .quests import QuestLog
        return self._facade("quests", lambda: QuestLog(self))

    def hud(self) -> "Hud":
        from .hud import Hud
        return self._facade("hud", lambda: Hud(self))

    def buffs(self) -> "Buffs":
        from .buffs import Buffs
        return self._facade("buffs", lambda: Buffs(self))

    def deposit_box(self) -> "DepositBox":
        from .deposit_box import DepositBox
        return self._facade("deposit_box", lambda: DepositBox(self))

    def loot(self) -> "LootInventory":
        from .deposit_box import LootInventory
        return self._facade("loot", lambda: LootInventory(self))

    def components(self, iface: int, comp: int = 0) -> "ComponentQuery":
        """A new query over the component tree under ``iface:comp`` (the interface's root by
        default). One ``get_interface_tree`` when a terminal first needs it."""
        from .component_query import ComponentQuery
        return ComponentQuery(self, iface, comp)

    def logout(self) -> bool:
        """Ask the agent to log out to the login screen (``logout``). Returns once queued: poll
        :meth:`login_state` (or the snapshot's game state) for the result. Right after a login
        the first logout click was once seen lost; wait a few ticks after logging in."""
        self._rpc.call("logout")
        return True

    def exit_to_lobby(self) -> bool:
        """Ask the agent to exit to the lobby (``exit_to_lobby``). Returns once queued."""
        self._rpc.call("exit_to_lobby")
        return True

    @property
    def draw(self) -> DebugDraw:
        """The debug-draw overlay (:mod:`botwithus.api.draw`): ``game.draw.set(draw.rect(...))``."""
        return self._draw

    def backpack(self) -> InventoryContainer:
        """The backpack, readable and clickable (:class:`~botwithus.api.inventory.InventoryContainer`)."""
        return backpack_container(self)

    def bank(self) -> InventoryContainer:
        """The bank. Published, and clickable, only while the bank is open."""
        return bank_container(self)

    def equipment(self) -> InventoryContainer:
        """Worn equipment."""
        return equipment_container(self)

    # --- state ---------------------------------------------------------------

    def snapshot(self) -> SnapshotView:
        """The current game snapshot (a fresh coherent frame per call)."""
        return self._snapshot()

    def login_state(self) -> dict[str, Any]:
        return self._rpc.call("get_login_state")

    def account_info(self) -> dict[str, Any]:
        return self._rpc.call("get_account_info")

    def current_world(self) -> int:
        return int(self._rpc.call("get_current_world").get("world_id", -1))

    # --- client reads: GE, worlds, hop (PUBLIC_PROTOCOL.md 6.2.1, 6.2.2) ------------------

    def ge_offers(self) -> tuple[GeOffer, ...]:
        """The eight Grand Exchange slots of market 0 (``get_ge_offers``). A slot is in use when
        :attr:`GeOffer.in_use` (``stage != 0``). Raises :class:`RpcError` ``not_available``
        when the client has no GE manager yet."""
        return ge_offers_from_reply(self._rpc.call("get_ge_offers", {"market": 0}))

    def world_list(self) -> WorldList:
        """The world-select list as the client holds it (``get_world_list``). An EMPTY list
        (:attr:`WorldList.is_fetched` False) means the game has not fetched it yet, not an
        error: every login through the lobby fetches it."""
        return world_list_from_reply(self._rpc.call("get_world_list"))

    def set_world(self, world_id: int) -> SetWorldResult:
        """Ask the agent to change world through the game's own UI (``set_world``), one call.
        A refusal is ``started`` False with the agent's ``reason``; it does not raise. Use
        :meth:`hop_world` to wait for the arrival."""
        if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id <= 0:
            raise ValueError(f"world_id must be a positive int, got {world_id!r}")
        return set_world_from_reply(self._rpc.call("set_world", {"world_id": world_id}))

    def hop_world(self, world_id: int, timeout_s: float = 30.0, poll_s: float = 0.3) -> HopResult:
        """``set_world``, then poll ``get_current_world`` and ``get_login_state`` until the
        client reports ``world_id`` with its state back at the lobby (20) or the world (30), or
        ``timeout_s`` passes. :attr:`HopResult.arrived` is the predicate; a refusal returns at
        once with ``started`` False and the agent's ``reason``. A GUI stop ends the wait
        (``stopped`` True). In the world a hop takes 3-6 s; the agent gives up after 15 s
        waiting for the world's row and 20 s waiting for the world."""
        start = time.monotonic()
        sent = self.set_world(world_id)
        if not sent.started:
            return HopResult(False, False, -1, -1, time.monotonic() - start, sent.reason)
        world, state = -1, -1
        while True:
            world, state = self._hop_poll(world, state)
            elapsed = time.monotonic() - start
            if world == world_id and state in (20, 30):
                return HopResult(True, True, world, state, elapsed)
            if elapsed >= timeout_s:
                return HopResult(False, True, world, state, elapsed)
            if not _pause(poll_s):
                return HopResult(False, True, world, state, elapsed, stopped=True)

    # --- target mode and "use X on Y" (PUBLIC_PROTOCOL.md 6.2.1, 6.3.1) --------------------

    def target_mode(self) -> TargetMode:
        """``get_target_mode``: :attr:`TargetMode.active` while an item is selected with Use
        (or a spell or ability is armed), with the item, its source component and slot."""
        return target_mode_from_reply(self._rpc.call("get_target_mode"))

    def use_on_component(self, source: ComponentRef, target: ComponentRef) -> bool:
        """Use ``source`` on ``target`` (SELECT_COMPONENT 25, then SELECT_COMPONENT_ITEM 58).

        Arms target mode on ``source``, waits until :meth:`target_mode` reads active with
        ``source`` as its source (at most :data:`USE_ON_ARM_TIMEOUT_S`; active alone could be an
        earlier arm), then queues the target action. The agent ends target mode
        itself after it. Returns True when both actions are queued (what the game makes of them
        is the server's answer), False when target mode never armed, and then the target action
        is NOT queued. Raises as :meth:`queue_action` does."""
        return self._use_on(source, GameAction(ActionType.SELECT_COMPONENT_ITEM, 0, target.sub,
                                               target.packed))

    def use_on_npc(self, source: ComponentRef, server_index: int) -> bool:
        """Use ``source`` on the NPC at ``server_index`` (25, then SELECT_NPC 8). As
        :meth:`use_on_component`."""
        return self._use_on(source, GameAction(ActionType.SELECT_NPC, server_index, 0, 0))

    def use_on_object(self, source: ComponentRef, loc_id: int, tile_x: int, tile_y: int) -> bool:
        """Use ``source`` on the loc ``loc_id`` at (``tile_x``, ``tile_y``) (25, then
        SELECT_OBJECT 2); take all three from the same snapshot row. As :meth:`use_on_component`."""
        return self._use_on(source, GameAction(ActionType.SELECT_OBJECT, loc_id, tile_x, tile_y))

    def use_item_on_item(self, slot: int, target_slot: int) -> bool:
        """Use the backpack item in ``slot`` on the one in ``target_slot`` (knife on logs)."""
        return self.use_on_component(_backpack(slot), _backpack(target_slot))

    def use_item_on_npc(self, slot: int, server_index: int) -> bool:
        """Use the backpack item in ``slot`` on an NPC."""
        return self.use_on_npc(_backpack(slot), server_index)

    def use_item_on_object(self, slot: int, loc_id: int, tile_x: int, tile_y: int) -> bool:
        """Use the backpack item in ``slot`` on a loc (an object)."""
        return self.use_on_object(_backpack(slot), loc_id, tile_x, tile_y)

    def _use_on(self, source: ComponentRef, target: GameAction) -> bool:
        self.queue_action(GameAction(ActionType.SELECT_COMPONENT, 0, source.sub, source.packed))
        deadline = time.monotonic() + USE_ON_ARM_TIMEOUT_S
        # Armed FROM source: active alone could be an earlier arm, and firing on it would use
        # the wrong thing.
        while not _is_armed_from(self.target_mode(), source):
            if time.monotonic() >= deadline or not _pause(USE_ON_POLL_S):
                return False
        self.queue_action(target)
        return True

    # --- projection (PUBLIC_PROTOCOL.md 6.2.1) ------------------------------------------

    def world_to_screen(self, tiles: Iterable[Any]) -> Projection:
        """Project tile centres to window client pixels with the agent's own projector, 1..64
        per call. Each tile is a :class:`Tile`, or ``(x, y)`` / ``(x, y, plane)``; a tile with no
        plane is taken on the local player's plane. :attr:`ScreenPoint.on_screen` is the
        predicate per point; another plane than the player's reads ``wrong_plane``."""
        points = [_tile_point(t) for t in tiles]
        if not 1 <= len(points) <= WORLD_TO_SCREEN_MAX:
            raise ValueError(f"world_to_screen takes 1..{WORLD_TO_SCREEN_MAX} tiles, got {len(points)}")
        reply = self._rpc.call("world_to_screen", {"points": points})
        return projection_from_reply(reply, len(points))

    def _hop_poll(self, world: int, state: int) -> tuple[int, int]:
        """One read of the world and the login state; a read that times out keeps the last."""
        try:
            world = self.current_world()
            state = int(self._rpc.call("get_login_state").get("state", -1))
        except MalformedReply:
            raise
        except RpcError as exc:
            if exc.agent_message not in _OUTCOME_UNKNOWN:
                raise
        return world, state

    def game_cycle(self) -> int:
        return int(self._rpc.call("get_game_cycle").get("cycle", 0))

    # --- action queue --------------------------------------------------------

    def queue_action(self, action: GameAction) -> None:
        """Queue one action. Raises :class:`ActionsRefused` (``queue_full``) when the agent
        has no room for it; only then is it certainly not queued. Any other error, such as
        :class:`ActionOutcomeUnknown` on a timeout, leaves the outcome unknown."""
        self._queue_call("queue_action", {
            "action_id": action.action_id,
            "param1": action.param1,
            "param2": action.param2,
            "param3": action.param3,
        })

    def queue_actions(self, actions: list[GameAction]) -> int:
        """Queue ``actions`` in order, in one call, with nothing else landing between them.

        All or nothing: the agent queues the whole batch and this returns ``len(actions)``,
        or it refuses the whole batch, queues none of it, and this raises
        :class:`ActionsRefused` (``queue_full``, ``batch_too_large`` past
        :data:`MAX_ACTION_BATCH`, ``action_id_required``, ``bad_param ...``).
        :class:`ActionsRefused` is the only error that means nothing was queued. After any
        other error the batch may or may not be queued: a timeout or an unreadable reply
        (:class:`ActionOutcomeUnknown`), a lost connection (``ConnectionError``), or any other
        :class:`RpcError` (:class:`MalformedReply` included).

        One exception: an agent older than NXTLibrary #67 (still protocol v25) could queue
        only the first ``n`` actions when its queue filled, and this then returns that ``n``.
        A current agent never does."""
        payload = [
            {"action_id": a.action_id, "param1": a.param1, "param2": a.param2, "param3": a.param3}
            for a in actions
        ]
        reply = self._queue_call("queue_actions", {"actions": payload})
        queued = reply.get("queued") if isinstance(reply, dict) else None
        is_count = isinstance(queued, int) and not isinstance(queued, bool)
        if not is_count or not 0 <= queued <= len(actions):
            raise MalformedReply("queue_actions",
                                 f"queued {queued!r} for a batch of {len(actions)}")
        return queued

    def _queue_call(self, method: str, params: dict[str, Any]) -> Any:
        """An action-queue RPC, with the agent's refusal and a timeout told apart."""
        try:
            return self._rpc.call(method, params)
        except MalformedReply:
            raise
        except RpcError as exc:
            msg = exc.agent_message
            if msg in _OUTCOME_UNKNOWN:
                raise ActionOutcomeUnknown(method, msg) from exc
            if msg.startswith(f"{method}: "):
                raise ActionsRefused(method, msg) from exc
            raise

    def action_queue_size(self) -> int:
        return int(self._rpc.call("get_action_queue_size").get("size", 0))

    def action_history(self, max_results: int = MAX_ACTION_HISTORY,
                       action_id: int | None = None) -> tuple[ActionHistoryEntry, ...]:
        """The actions the agent dispatched, **oldest first**, from its ring of the last
        :data:`MAX_ACTION_HISTORY` (``get_action_history``).

        ``max_results`` (1..128) caps the count, and it keeps the OLDEST entries in the ring,
        not the newest: ask for all 128 and take the tail for "the last few". ``action_id``
        keeps only that action id. The ring is per agent, not per script, and
        :meth:`clear_action_queue` does not empty it.

        :raises ValueError: for a ``max_results`` outside 1..128 (the agent would clamp it
            silently) or a negative ``action_id`` (the agent reads that as "no filter").
        """
        if (isinstance(max_results, bool) or not isinstance(max_results, int)
                or not 1 <= max_results <= MAX_ACTION_HISTORY):
            raise ValueError(f"max_results must be 1..{MAX_ACTION_HISTORY}, got {max_results!r}")
        params: dict[str, Any] = {"max_results": max_results}
        if action_id is not None:
            if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id < 0:
                raise ValueError(f"action_id must be a non-negative int, got {action_id!r}")
            params["action_id_filter"] = action_id
        return action_history_from_reply(self._rpc.call("get_action_history", params))

    def last_action_time(self) -> int | None:
        """When the agent last dispatched an action, on the same clock as
        :attr:`ActionHistoryEntry.timestamp` (milliseconds since the game machine booted),
        or None when it has dispatched none since it started or its queue was last cleared."""
        reply = self._rpc.call("get_last_action_time")
        stamp = reply.get("timestamp") if isinstance(reply, dict) else None
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise MalformedReply("get_last_action_time", f"timestamp {stamp!r}")
        return None if stamp == 0 else stamp

    def clear_action_queue(self) -> None:
        self._rpc.call("clear_action_queue")

    def actions_blocked(self) -> bool:
        return bool(self._rpc.call("are_actions_blocked").get("blocked", False))

    def set_actions_blocked(self, blocked: bool) -> None:
        self._rpc.call("set_actions_blocked", {"blocked": blocked})

    # --- breaks --------------------------------------------------------------

    def schedule_break(self, duration_seconds: float) -> None:
        """Hold the action queue for ``duration_seconds`` seconds, starting now.

        Replaces any break already running. The agent counts the break in milliseconds,
        so the value is converted (and rounded to the nearest millisecond) before it is
        sent: ``schedule_break(300)`` is a five-minute break. Fractions are allowed.
        Raises ``ValueError`` below 1 ms or above :data:`MAX_BREAK_SECONDS`, and
        ``TypeError`` for a non-number; nothing is sent in either case. The agent then
        pushes a :class:`~botwithus.api.events.BreakStarted` event (whole seconds) and,
        when the break ends or :meth:`interrupt_break` is called, a ``BreakEnded``.
        """
        self._rpc.call("schedule_break", {"duration": break_duration_ms(duration_seconds)})

    def interrupt_break(self) -> None:
        self._rpc.call("interrupt_break")

    # --- variables -----------------------------------------------------------

    # Varps and varbits: see botwithus.api.variables for what each state means. Decide on
    # the state, never on the value -- a set varp can hold -1, and so can a varp at its
    # default.

    def _vars(self) -> "VariableReader":
        if self._variables is None:
            raise RuntimeError("this GameApi has no variable reader (construct it with one)")
        return self._variables

    def read_varp(self, var_id: int) -> VarpRead:
        """The varp's state and value (see :class:`~botwithus.api.variables.VarpState`)."""
        return self._vars().read_varps([var_id])[0]

    def read_varps(self, ids: Iterable[int]) -> list[VarpRead]:
        """One :class:`VarpRead` per id, in order. Any number of ids."""
        return self._vars().read_varps(ids)

    def read_varbit(self, varbit_id: int) -> VarbitRead:
        """The varbit's base state and decoded value."""
        return self._vars().read_varbits([varbit_id])[0]

    def read_varbits(self, ids: Iterable[int]) -> list[VarbitRead]:
        """One :class:`VarbitRead` per id, in order. Any number of ids."""
        return self._vars().read_varbits(ids)

    def varp_state(self, var_id: int) -> VarpState:
        """Just the state: SET, DEFAULT_NOT_SET_CLIENTSIDE, NO_SUCH_VARP or UNAVAILABLE."""
        return self.read_varp(var_id).state

    def varp(self, var_id: int) -> int:
        """The value the game reads: the stored value, or the varp's default when the client
        holds none, or ``-1`` for no such varp / a failed read. ``-1`` is also a legal stored
        value -- use :meth:`read_varp` to tell them apart. A LONG varp's low 32 bits."""
        return self.read_varp(var_id).value

    def varp_long(self, var_id: int) -> int:
        """Like :meth:`varp`, at full width: all 64 bits of a LONG varp."""
        return self.read_varp(var_id).value64

    def varps(self, ids: list[int]) -> list[int]:
        """:meth:`varp` for each id, in order."""
        return [r.value for r in self.read_varps(ids)]

    def varbit(self, varbit_id: int) -> int:
        """The varbit's value as the game reads it, or ``-1`` when it has none."""
        return self.read_varbit(varbit_id).value

    def varc_int(self, var_id: int) -> int:
        return int(self._rpc.call("get_varc_int", {"var_id": var_id}).get("value", -1))

    def varc_string(self, var_id: int) -> str:
        return str(self._rpc.call("get_varc_string", {"var_id": var_id}).get("value", ""))

    # --- local player health -------------------------------------------------

    def local_health(self) -> tuple[int, int] | None:
        """Local life points as ``(current, max)``, or ``None`` when unknown.

        ``None``, with no round-trip and whatever was cached, on a frame with no player
        (``LocalPlayer.valid`` False): there is nobody for a varp to describe.

        One batched :meth:`read_varps` per server tick, cached for the rest of it: a
        caller that asks ten times while deciding what to do this tick costs one
        round-trip, and the answer cannot change mid-tick anyway.

        ``None`` rather than a sentinel pair. The Java host returns
        ``HEALTH_UNKNOWN`` because a record field cannot be absent; Python can
        say "no reading", and that is worth saying, because every caller here
        gates on the ratio and ``(-1, -1)`` divides just as happily as real
        numbers.

        Four behaviours mirrored from ``JBotWithUsV2``
        ``core/.../impl/GameAPIImpl.java`` (``readHealth`` / ``fetchHealth``):

        * keyed on the SERVER tick, not the game cycle or the publish sequence
        * only a KNOWN reading is cached, so one failed call does not pin
          "unknown" for the rest of the tick
        * an RPC failure (:class:`RpcError`) degrades to unknown instead of
          propagating: health is one field among many and a handler error should
          not take the caller down. A dead transport is NOT degraded: a lost pipe
          or a client exit raises ``ConnectionError`` (``ClientExited`` for the
          exit), as below
        * a short result is unknown, NOT one value shifted into the other's slot

        The last two matter most. Both would otherwise produce a confidently
        wrong HP, and for anything that gates on HP a wrong number is worse than
        no number.

        A reading is KNOWN only when both varps are
        :attr:`~botwithus.api.variables.VarpState.SET`, or
        :attr:`~botwithus.api.variables.VarpState.DEFAULT_NOT_SET_CLIENTSIDE` with a
        verified default. Anything else -- UNAVAILABLE (lobby, entering the world),
        NO_SUCH_VARP, or a default the host could not yet confirm from the cache --
        is ``None`` and is not cached, so the next call re-reads. The native host
        reports a failed read per id as UNAVAILABLE with value ``-1`` rather than
        raising, so without this gate ``(-1, -1)`` would be cached as a real
        reading. Expect ``None`` for the first moments after attach while the
        host's varp defaults warm up.

        One deliberate narrowing of the Java version, which catches
        ``RuntimeException`` wholesale: only :class:`RpcError` is swallowed here.
        A dead transport raises :class:`ConnectionError` and is allowed through,
        because reporting "unknown health" while the connection is gone would
        hide a fatal condition behind a value that looks merely unavailable. That
        includes a read after the client exited: it raises
        :class:`~botwithus.core.native.ClientExited`, which a worker or the CLI
        runner treats as a clean stop, and a read after the connection closed
        raises :class:`~botwithus.core.native.HostDetached`.

        :class:`~botwithus.core.native.NativeUnavailable` also propagates: a
        ``bwu_host`` that predates varp state reads cannot answer health at all,
        and saying so loudly beats an eternal ``None``.
        """
        snap = self.snapshot()
        if not snap.self_player.valid:
            return None
        tick = snap.server_tick
        cached = self._health
        if cached is not None and cached[0] == tick:
            return cached[1], cached[2]

        sample = self._fetch_health()
        if sample is not None:
            self._health = (tick, sample[0], sample[1])
        return sample

    def _fetch_health(self) -> tuple[int, int] | None:
        """One batched varp read; ``None`` on any shape or state this cannot trust."""
        wanted = (VARP_CURRENT_HEALTH, VARP_MAX_HEALTH)
        try:
            reads = self.read_varps(wanted)
        except RpcError:
            return None
        # A host that truncated or reordered the batch leaves both unknown rather
        # than letting max slide into current's place.
        if tuple(r.id for r in reads) != wanted:
            return None
        if not all(_is_known(r) for r in reads):
            return None
        return reads[0].value, reads[1].value

    # --- interfaces / components ---------------------------------------------

    def component(self, iface: int, comp: int) -> dict[str, Any]:
        """The agent's component map, passed through as a dict.

        Visibility (agent protocol v24): ``visible`` is the component's own visibility (its
        slot flag byte is 0); ``visible_global`` is ``visible`` at every level up to a root,
        crossing into a mounted sub-interface's host, which is what "on screen" means;
        ``slot_flags`` is the raw byte (-1 when no slot was found or the channel cannot read
        it). ``hidden`` is now exactly ``0 if visible else 1`` for every category, including
        layers, sprites, models and boxes that used to read -1. A slot that is not found on a
        readable channel reads ``slot_flags`` -1, ``visible`` False and ``hidden`` 1; ``hidden``
        is -1 only for a component that was not found (``iface == -1``) or on a client channel
        without the visibility offsets. ``parent_global`` is the packed ``(iface << 16) | comp``
        of the parent the client walks next, or -1. Layers carry ``scrollable`` True with
        ``scroll_x`` / ``scroll_y`` (clamped as the client clamps them) and ``scroll_w`` /
        ``scroll_h`` (content size); every other category reads False and zeros.
        """
        return self._rpc.call("get_component", {"iface": iface, "comp": comp})

    def components(self, targets: list[tuple[int, int]]) -> list[dict[str, Any]]:
        payload = [[i, c] for i, c in targets]
        return list(self._rpc.call("get_components", {"targets": payload}).get("components", []))

    def interface_tree(self, iface: int, comp: int) -> dict[str, Any]:
        return self._rpc.call("get_interface_tree", {"iface": iface, "comp": comp})

    def find_component_at(self, x: int, y: int) -> dict[str, Any]:
        """The topmost on-screen component at client pixel (``x``, ``y``), as a component map.

        Only components that are on screen can be picked (agent protocol v24): the agent skips
        any node whose ``visible_global`` is false, and with it that node's whole subtree, so
        text inside a hidden panel is never returned. On a client channel without the
        visibility offsets every node reads ``visible_global`` True and stays pickable.
        """
        return self._rpc.call("find_component_at", {"x": x, "y": y})

    def component_ops(self, iface: int, comp: int, sub: int = NO_SUB) -> ComponentOps:
        """The live right-click ops of ``iface:comp``, or of its ``sub`` slot (an item grid's
        slot index), from ``get_component_ops`` (PUBLIC_PROTOCOL.md 6.7.2).

        Raises :class:`RpcError` with ``agent_message`` ``get_component_ops: not_found`` when the
        component is not open (the backpack at the lobby, a slot past the grid) and
        ``get_component_ops: not_ready`` when the game thread did not answer. An empty slot
        answers no ops, not an error. :class:`ValueError` for an id out of range, unsent."""
        target = InterfaceComponent(self, iface, comp, sub)
        reply = self._rpc.call("get_component_ops",
                               {"iface": target.iface, "comp": target.comp, "sub": target.sub})
        return ComponentOps.from_reply(reply)

    def static_children(self, iface: int, comp: int) -> list[int]:
        """The component ids of ``iface:comp``'s children baked into the cache layout
        (``get_static_children``). Empty for a component that is not open, and for one that
        is not a container (a button, a text). Pass each id to :meth:`component`."""
        return self._children("get_static_children", iface, comp)

    def dynamic_children(self, iface: int, comp: int) -> list[int]:
        """One entry per child ``iface:comp`` created at run time (``get_dynamic_children``).

        Use it for a COUNT only. A dynamic child carries its parent's component id and is told
        apart by its sub id, which this list does not report, so every entry is the parent's
        own id (or -1). Use :meth:`dynamic_children_with_sub` for the children's sub ids, and
        :meth:`interface_tree` for their data."""
        return self._children("get_dynamic_children", iface, comp)

    def static_children_with_sub(self, iface: int, comp: int) -> tuple[Child, ...]:
        """The static children of ``iface:comp`` as :class:`Child` ``(comp, sub)``
        (``get_static_children {withSub: true}``)."""
        return self._children_with_sub("get_static_children", iface, comp)

    def dynamic_children_with_sub(self, iface: int, comp: int) -> tuple[Child, ...]:
        """The dynamic children of ``iface:comp`` as :class:`Child` ``(comp, sub)``
        (``get_dynamic_children {withSub: true}``). ``comp`` is the parent's id; ``sub`` tells the
        children apart and is the ``param2`` of a COMPONENT or SELECT_COMPONENT_ITEM action on
        that child (for the backpack, the slot)."""
        return self._children_with_sub("get_dynamic_children", iface, comp)

    def _children_with_sub(self, method: str, iface: int, comp: int) -> tuple[Child, ...]:
        reply = self._rpc.call(method, {"iface": iface, "comp": comp, "withSub": True})
        return children_from_reply(method, reply)

    def _children(self, method: str, iface: int, comp: int) -> list[int]:
        reply = self._rpc.call(method, {"iface": iface, "comp": comp})
        children = reply.get("children") if isinstance(reply, dict) else None
        if not isinstance(children, list) or not all(_is_int(c) for c in children):
            raise MalformedReply(method, f"expected a list of ids, got {reply!r}")
        return list(children)

    def interface_component(self, iface: int, comp: int,
                            sub: int = NO_SUB) -> InterfaceComponent:
        """A handle on ``iface:comp`` (or one ``sub`` slot of it) that can
        :meth:`~botwithus.api.components.InterfaceComponent.interact`. Nothing is read until a
        method on it is called."""
        return InterfaceComponent(self, iface, comp, sub)

    def interact_component(self, iface: int, comp: int, option_name: str,
                           sub: int = NO_SUB) -> bool:
        """Click the op named ``option_name`` on ``iface:comp`` (or its ``sub`` slot).

        True when the action was queued. False, with nothing queued, when the component is not
        open (``get_component_ops: not_found``) or lists no op with that name. Other errors
        raise, as :meth:`queue_action` does."""
        try:
            return InterfaceComponent(self, iface, comp, sub).interact(option_name)
        except RpcError as exc:
            if exc.agent_message == "get_component_ops: not_found":
                return False
            raise

    # --- obj vars / spot anims -----------------------------------------------

    def obj_vars(self, inv_id: int, slot: int | None = None) -> tuple[SlotObjVars, ...]:
        """The per-item obj vars of inventory ``inv_id`` (``get_obj_vars``): one
        :class:`~botwithus.api.rpc_types.SlotObjVars` per slot whose item carries any
        (augmentation XP, charges...).

        ``slot`` limits it to that one slot. A slot with no obj vars, an empty slot, an
        inventory that is not loaded, or a read outside the world all answer an empty tuple:
        the agent does not tell those apart. :class:`ValueError` for a negative ``inv_id`` or
        ``slot``, unsent (the agent would read a bad slot as "every slot")."""
        if not _is_int(inv_id) or inv_id < 0:
            raise ValueError(f"inv_id must be a non-negative int, got {inv_id!r}")
        params: dict[str, Any] = {"inv_id": inv_id}
        if slot is not None:
            if not _is_int(slot) or slot < 0:
                raise ValueError(f"slot must be a non-negative int, got {slot!r}")
            params["slot"] = slot
        return obj_vars_from_reply(self._rpc.call("get_obj_vars", params))

    def slot_obj_vars(self, inv_id: int, slot: int) -> dict[int, int]:
        """``{var_id: value}`` for one slot; empty when it carries none (see :meth:`obj_vars`)."""
        for entry in self.obj_vars(inv_id, slot):
            if entry.slot == slot:
                return dict(entry.vars)
        return {}

    def spot_anims(self) -> tuple[WorldSpotAnim, ...]:
        """Spot anims playing on tiles in the scene (``query_spot_anims``), at most 256.

        Only world spot anims: one on an NPC or a player is its ``spot_anim_id`` in the
        snapshot, and arrives as a :class:`~botwithus.api.events.SpotAnim` event. Outside the
        world, or when the game thread does not answer within 500 ms, this is empty."""
        return spot_anims_from_reply(self._rpc.call("query_spot_anims"))

    # --- players ------------------------------------------------------------

    def player_appearance(self, server_index: int) -> PlayerAppearance:
        """The applied appearance of the loaded player at ``server_index`` (v24).

        Raises :class:`RpcError` with ``agent_message`` set to the agent's code:
        ``not_in_game``, ``no_player`` (index out of range or nothing loaded there),
        ``appearance_not_loaded`` (the client has not decoded it yet; normal for a player who
        just came into view), ``bad_appearance_record``, ``unsupported_on_client_channel`` or
        ``game_thread_timeout``.
        """
        reply = self._rpc.call("get_player_appearance", {"serverIndex": int(server_index)})
        return PlayerAppearance.from_reply(reply)

    # --- entity reads (v25) --------------------------------------------------

    def _entity_read(self, method: str, params: dict[str, Any] | None, result: Any) -> Any:
        try:
            reply = self._rpc.call(method, params)
        except RpcError as exc:
            status = ENTITY_REFUSALS.get(exc.agent_message)
            if status is None:
                raise
            return result(status=status)
        try:
            return result.from_reply(reply)
        except (KeyError, TypeError) as exc:
            raise MalformedReply(method, f"malformed reply: {exc!r}") from exc

    @staticmethod
    def _entity_target(kind: str, index: int) -> dict[str, Any]:
        if kind not in _ENTITY_KINDS:
            raise ValueError(f"kind must be one of {_ENTITY_KINDS}, not {kind!r}")
        return {"targetType": kind, "serverIndex": int(index)}

    def hint_arrows(self) -> HintArrowList:
        """Every hint arrow, in the manager's 8 slots and its owner tree, up to 32 (v25).

        Check ``.ok`` before trusting ``.arrows``. The result object is always truthy, and a
        refusal has empty ``.arrows``, so ``if game.hint_arrows():`` tests nothing and an empty
        tuple alone does not mean "no arrows". On a refusal ``.status`` is the agent's code:
        ``NOT_IN_WORLD``, ``CHANNEL_UNSUPPORTED`` or ``GAME_THREAD_TIMEOUT``. ``.unsupported`` is
        True for ``CHANNEL_UNSUPPORTED`` alone, the normal answer on the beta client channel.
        ``.truncated`` means the list is partial.

        Raises :class:`RpcError` when there is no answer to give: a host-side ``rpc timeout``, a
        message the contract does not list, or :class:`MalformedReply`. A dead transport raises
        :class:`ConnectionError`.

        Only ``target_type == "tile"`` arrows have a ``tile``; use ``target_index`` for npc and
        player arrows. ``entities.HintArrows`` is the query form.
        """
        return self._entity_read("query_hint_arrows", None, HintArrowList)

    def overhead_text(self, kind: str, index: int) -> OverheadText:
        """The text above an entity (v25). ``kind`` is ``"npc"`` or ``"player"`` and ``index`` its
        server index; any other ``kind`` raises :class:`ValueError` without a call.

        Check ``.ok`` before trusting ``.text``. The result object is always truthy. ``.text`` is
        None both when the entity is silent (``.ok`` True, not an error) and on a refusal (``.ok``
        False). On a refusal ``.status`` is the agent's code: ``NOT_IN_WORLD``, ``NOT_FOUND``
        (index out of range or nothing loaded there; routine, since an entity can despawn after
        the snapshot), ``CHANNEL_UNSUPPORTED`` or ``GAME_THREAD_TIMEOUT``. ``.unsupported`` is True
        for ``CHANNEL_UNSUPPORTED`` alone, the normal answer on the beta client channel.

        Raises as :meth:`hint_arrows` does.
        """
        return self._entity_read("get_overhead_text", self._entity_target(kind, index),
                                 OverheadText)

    def headbars(self, kind: str, index: int) -> HeadbarList:
        """The headbar entries on an entity, slot by slot with no liveness filter, up to 8 per
        slot and 64 in total (v25). ``kind`` and ``index`` as :meth:`overhead_text`.

        Check ``.ok`` before trusting ``.headbars``. The result object is always truthy, and
        ``.headbars`` is empty both for an entity with no bars (``.ok`` True) and on a refusal
        (``.ok`` False). On a refusal ``.status`` is the agent's code: ``NOT_IN_WORLD``,
        ``NOT_FOUND``, ``CHANNEL_UNSUPPORTED`` or ``GAME_THREAD_TIMEOUT``, as
        :meth:`overhead_text`. ``.unsupported`` is True for ``CHANNEL_UNSUPPORTED`` alone, the
        normal answer on the beta client channel. ``.truncated`` means the list is partial.

        Raises as :meth:`hint_arrows` does.
        """
        return self._entity_read("get_headbars", self._entity_target(kind, index), HeadbarList)

    # --- scripts (CS2) -------------------------------------------------------

    def script_handle(self, script_id: int) -> int:
        return int(self._rpc.call("get_script_handle", {"script_id": script_id}).get("handle", 0))

    def execute_script(self, handle: int, int_args: list[int] | None = None,
                       returns: list[str] | None = None,
                       string_args: list[str] | None = None) -> list[Any]:
        """Run a script by handle. It receives its ints in ``int_args`` order and its strings in
        ``string_args`` order. At most 16 args together and 255 UTF-8 bytes per string, refused
        here with :class:`ValueError` before anything is sent. ``returns`` names the kinds to
        read back ("int", "long", "string"); a string return reads ``""``. An empty list back
        means the run failed (a bad handle). The strings ``"event_opbase"`` and ``"event_text"``
        arrive as ``""``: the client fills them from an event this call does not have."""
        params = _script_params(handle, int_args, string_args, returns)
        return list(self._rpc.call("execute_script", params).get("returns", []))

    def destroy_script_handle(self, handle: int) -> None:
        self._rpc.call("destroy_script_handle", {"handle": handle})

    # --- session / login -----------------------------------------------------

    def login_to_lobby(self) -> dict[str, Any]:
        return self._rpc.call("login_to_lobby")

    def set_auto_login(self, enabled: bool) -> None:
        self._rpc.call("set_auto_login", {"enabled": enabled})

    def auto_login(self) -> bool:
        return bool(self._rpc.call("get_auto_login").get("enabled", False))

    # --- low-level input -----------------------------------------------------

    def send_key(self, vk: int | None = None, char: int | None = None, extended: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"extended": extended}
        if vk is not None:
            params["vk"] = vk
        if char is not None:
            params["char"] = char
        return self._rpc.call("send_key", params)

    def send_click(self, x: int, y: int, button: int = 0) -> bool:
        return bool(self._rpc.call("send_click", {"x": x, "y": y, "button": button}).get("sent", False))

    # --- escape hatch --------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Raw RPC for methods without a typed wrapper yet."""
        return self._rpc.call(method, params)

    def list_methods(self) -> list[str]:
        return list(self._rpc.call("rpc.list_methods"))
