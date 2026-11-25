"""The Lodestone Network: which lodestones are unlocked, teleporting to one, and repeating the
last one.

Mirrors the Java host's ``world/Lodestone`` and ``LodestoneNetwork`` (JBotWithUsV2, phase J2).
Every click goes through the action queue; no camera or desktop input. The minimap's home
button opens the network ("Lodestone Network"); its second op repeats the last lodestone
teleport and is named after that lodestone ("Teleport Burthorpe"), not "Previous Destination".

**Arrival** is judged from the snapshot: the player has to end at least
:data:`ARRIVAL_DISTANCE` tiles from where it started (or on another plane).

**Settle and retry.** Right after an arrival the game refuses the network with "Please finish
what you're doing before opening the lodestone network." So :meth:`LodestoneNetwork.teleport`
waits for the landing animation to end before it returns, and opening the network is tried
:data:`OPEN_ATTEMPTS` times.

These calls block the calling thread (through the stop- and pause-aware waits in
:mod:`botwithus.api.timing`).
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, Callable

from . import timing
from .actions import component_click
from .components import ComponentOps
from .gameval import GamevalIndex, GamevalRef, GamevalType, component_ref

if TYPE_CHECKING:
    from .game import GameApi
    from .snapshot_types import LocalPlayer

NETWORK_INTERFACE = 1092       # THAS_LODESTONE_MAP
MINIMAP = 1465
HOME_BUTTON = 34               # TOPLEVEL_V2_MINIMAP__HOME_TELEPORT (in 1465)
ARRIVAL_DISTANCE = 20
OPEN_TIMEOUT_MS = 6_000
OPEN_ATTEMPTS = 2
SETTLE_TIMEOUT_MS = 6_000
ARRIVAL_TIMEOUT_MS = 20_000
OPEN_OPTION = "Lodestone Network"
PREVIOUS_OPTION_PREFIX = "Teleport "

_NO_ANIMATION = -1
_OP_TELEPORT = 1
_NO_SUB = -1
_UNLOCKED = 1
BUTTON_PREFIX = "THAS_LODESTONE_MAP__"
NETWORK_REF = GamevalRef(GamevalType.INTERFACE, "THAS_LODESTONE_MAP", NETWORK_INTERFACE)
HOME_BUTTON_REF = component_ref("TOPLEVEL_V2_MINIMAP__HOME_TELEPORT", MINIMAP, HOME_BUTTON)

# Each lodestone's (button suffix, unlock varbit gameval), as the Java host's Lodestone names
# them; the numbers in the enum are the fallbacks (native ABI 8).
_GAMEVALS: dict[str, tuple[str, str]] = {
    "AL_KHARID": ("AL_KHARID", "THAS_ACTIVE_AL_KHARID"),
    "ANACHRONIA": ("DINO_ISLAND", "DINO_BASE_CAMP_BUILDING_TIER_LODESTONE"),
    "ARDOUGNE": ("ARDOUGNE", "THAS_ACTIVE_ARDOUGNE"),
    "ASHDALE": ("ASHDALE", "THAS_ACTIVE_ASHDALE"),
    "BANDIT_CAMP": ("ANCIENTS", "DESERTTREASURE"),
    "BURTHORPE": ("BURTHORPE", "THAS_ACTIVE_BURTHORPE"),
    "CANIFIS": ("CANIFIS", "THAS_ACTIVE_CANIFIS"),
    "CATHERBY": ("CATHERBY", "THAS_ACTIVE_CATHERBY"),
    "CITY_OF_UM": ("CITYOFUM", "THAS_ACTIVE_CITYOFUM"),
    "DRAYNOR_VILLAGE": ("DRAYNOR", "THAS_ACTIVE_DRAYNOR"),
    "EAGLES_PEAK": ("EAGLES", "THAS_ACTIVE_EAGLES"),
    "EDGEVILLE": ("EDGEVILLE", "THAS_ACTIVE_EDGEVILLE"),
    "FALADOR": ("FALADOR", "THAS_ACTIVE_FALADOR"),
    "FORT_FORINTHRY": ("FORT", "PLAYER_FORT_LODESTONE"),
    "FREMENNIK_PROVINCE": ("FREMENNIK", "THAS_ACTIVE_FREMENNIK"),
    "KARAMJA": ("KARAMJA", "THAS_ACTIVE_KARAMJA"),
    "LUMBRIDGE": ("LUMBRIDGE", "THAS_ACTIVE_LUMBRIDGE"),
    "LUNAR_ISLE": ("LUNAR", "LUNAR_QUEST_MAIN"),
    "MENAPHOS": ("MENAPHOS", "THAS_ACTIVE_MENAPHOS"),
    "OOGLOG": ("OOGLOG", "THAS_ACTIVE_OOGLOG"),
    "PORT_SARIM": ("PORT_SARIM", "THAS_ACTIVE_PORT_SARIM"),
    "PRIFDDINAS": ("PRIFDDINAS", "THAS_ACTIVE_PRIFDDINAS"),
    "SEERS_VILLAGE": ("SEERS", "THAS_ACTIVE_SEERS"),
    "TAVERLEY": ("TAVERLEY", "THAS_ACTIVE_TAVERLEY"),
    "TIRANNWN": ("TIRRANWN", "THAS_ACTIVE_TIRRANWN"),
    "VARROCK": ("VARROCK", "THAS_ACTIVE_VARROCK"),
    "WENDLEWICK": ("WENDLEWICK", "THAS_ACTIVE_WENDLEWICK"),
    "WILDERNESS_CRATER": ("WILDERNESS", "THAS_ACTIVE_WILDERNESS"),
    "YANILLE": ("YANILLE", "THAS_ACTIVE_YANILLE"),
}


class Lodestone(enum.Enum):
    """``(network button component in 1092, unlock varbit, unlocked at least)``. Most have their
    own ``THAS_ACTIVE_*`` flag; four are gated on other state (quest progress for Lunar Isle and
    the Bandit Camp, a build tier for Anachronia and Fort Forinthry)."""

    AL_KHARID = (10, 28, _UNLOCKED)
    ANACHRONIA = (24, 44270, _UNLOCKED)        # DINO_BASE_CAMP_BUILDING_TIER_LODESTONE
    ARDOUGNE = (11, 29, _UNLOCKED)
    ASHDALE = (33, 22430, _UNLOCKED)
    BANDIT_CAMP = (8, 9482, 15)                # DESERTTREASURE
    BURTHORPE = (12, 30, _UNLOCKED)
    CANIFIS = (26, 18523, _UNLOCKED)
    CATHERBY = (13, 31, _UNLOCKED)
    CITY_OF_UM = (35, 53270, _UNLOCKED)
    DRAYNOR_VILLAGE = (14, 32, _UNLOCKED)
    EAGLES_PEAK = (27, 18524, _UNLOCKED)
    EDGEVILLE = (15, 33, _UNLOCKED)
    FALADOR = (16, 34, _UNLOCKED)
    FORT_FORINTHRY = (22, 52518, _UNLOCKED)    # PLAYER_FORT_LODESTONE
    FREMENNIK_PROVINCE = (28, 18525, _UNLOCKED)
    KARAMJA = (29, 18526, _UNLOCKED)
    LUMBRIDGE = (17, 35, _UNLOCKED)
    LUNAR_ISLE = (9, 10236, 100)               # LUNAR_QUEST_MAIN
    MENAPHOS = (23, 36173, _UNLOCKED)
    OOGLOG = (30, 18527, _UNLOCKED)
    PORT_SARIM = (18, 36, _UNLOCKED)
    PRIFDDINAS = (34, 24967, _UNLOCKED)
    SEERS_VILLAGE = (19, 37, _UNLOCKED)
    TAVERLEY = (20, 38, _UNLOCKED)
    TIRANNWN = (31, 18528, _UNLOCKED)
    VARROCK = (21, 39, _UNLOCKED)
    WENDLEWICK = (41, 60739, _UNLOCKED)
    WILDERNESS_CRATER = (32, 18529, _UNLOCKED)
    YANILLE = (25, 40, _UNLOCKED)

    @property
    def button_ref(self) -> GamevalRef:
        return component_ref(BUTTON_PREFIX + _GAMEVALS[self.name][0], NETWORK_INTERFACE, self.value[0])

    @property
    def unlock_ref(self) -> GamevalRef:
        return GamevalRef(GamevalType.VARBIT, _GAMEVALS[self.name][1], self.value[1])

    @property
    def button(self) -> int:
        """The button's component in the network window, by its gameval name."""
        return self.button_ref.component()[1]

    @property
    def unlock_varbit(self) -> int:
        """The unlock varbit, by its gameval name; the fallback id when it does not resolve."""
        return self.unlock_ref.resolve()

    def is_unlocked_by(self, varbit_value: int) -> bool:
        return varbit_value >= self.value[2]


class TeleportResult(enum.Enum):
    ARRIVED = "arrived"
    LOCKED = "locked"
    NETWORK_UNAVAILABLE = "network_unavailable"
    TIMED_OUT = "timed_out"
    NOT_IN_GAME = "not_in_game"


def has_arrived(start: "LocalPlayer", now: "LocalPlayer") -> bool:
    moved = max(abs(now.tile_x - start.tile_x), abs(now.tile_y - start.tile_y))
    return moved >= ARRIVAL_DISTANCE or (now.plane != start.plane and moved > 0)


class LodestoneNetwork:
    """Lodestone reads and teleports for one game connection."""

    def __init__(self, game: "GameApi", ctx: Any = None) -> None:
        self._game = game
        self._ctx = ctx  # the ScriptContext whose stop/pause the waits honour (or the bound one)

    def is_unlocked(self, lodestone: Lodestone) -> bool:
        return lodestone.is_unlocked_by(self._game.varbit(lodestone.unlock_varbit))

    def unlocked(self) -> list[Lodestone]:
        reads = self._game.read_varbits([l.unlock_varbit for l in Lodestone])
        return [l for l, r in zip(Lodestone, reads) if l.is_unlocked_by(r.value)]

    def is_open(self) -> bool:
        return self._game.snapshot().is_interface_open(NETWORK_REF.resolve())

    def open(self) -> bool:
        """Press the home button's "Lodestone Network". False when it lists no such op."""
        return self._press_home(lambda ops: ops.op_named(OPEN_OPTION))

    def teleport(self, lodestone: Lodestone,
                 timeout_ms: int = ARRIVAL_TIMEOUT_MS) -> TeleportResult:
        """Open the network if needed, click ``lodestone``, and wait up to ``timeout_ms`` for
        the arrival and then for the landing animation to end."""
        start = self._self()
        if start is None:
            return TeleportResult.NOT_IN_GAME
        if not self.is_unlocked(lodestone):
            return TeleportResult.LOCKED
        if not self._ensure_open():
            return TeleportResult.NETWORK_UNAVAILABLE
        self._game.queue_action(component_click(*lodestone.button_ref.component(),
                                                option=_OP_TELEPORT))
        return self._await_arrival(start, timeout_ms)

    def teleport_to_previous(self, timeout_ms: int = ARRIVAL_TIMEOUT_MS) -> TeleportResult:
        """Repeat the last lodestone teleport (the home button's "Teleport <lodestone>"); before
        any lodestone teleport this is the home teleport to Lumbridge."""
        start = self._self()
        if start is None:
            return TeleportResult.NOT_IN_GAME
        if not self._press_home(_previous_op):
            return TeleportResult.NETWORK_UNAVAILABLE
        return self._await_arrival(start, timeout_ms)

    def _ensure_open(self) -> bool:
        for _ in range(OPEN_ATTEMPTS):
            if self.is_open():
                return True
            if not self.open():
                return False
            timing.delay_until(self.is_open, OPEN_TIMEOUT_MS, ctx=self._ctx)
        return self.is_open()

    def _await_arrival(self, start: "LocalPlayer", timeout_ms: int) -> TeleportResult:
        def arrived() -> bool:
            now = self._self()
            return now is not None and has_arrived(start, now)

        if not timing.delay_until(arrived, timeout_ms, ctx=self._ctx):
            return TeleportResult.TIMED_OUT

        def settled() -> bool:
            now = self._self()
            return now is not None and now.animation_id == _NO_ANIMATION

        timing.delay_until(settled, SETTLE_TIMEOUT_MS, ctx=self._ctx)
        return TeleportResult.ARRIVED

    def _press_home(self, pick: Callable[[ComponentOps], "int | None"]) -> bool:
        from ..core.rpc import RpcError
        try:
            ops = self._game.component_ops(*HOME_BUTTON_REF.component())
        except RpcError:
            return False
        op = pick(ops)
        if op is None:
            return False
        self._game.queue_action(component_click(*HOME_BUTTON_REF.component(), option=op))
        return True

    def _self(self) -> "LocalPlayer | None":
        snap = self._game.snapshot()
        me = snap.self_player
        return me if snap.in_game and me.valid else None


def _previous_op(ops: ComponentOps) -> int | None:
    return next((o.op for o in ops.ops if o.name.startswith(PREVIOUS_OPTION_PREFIX)), None)
