"""SceneWalker -- Python-driven route execution over the native host's path surface.

The pathfinder (worldwalker) lives in the native host now, not in a ctypes binding here:
``NativeHost.path(x, y, plane)`` plans a route from the player's live tile to the goal and
hands back walk/transition steps. This keeps worldwalker off the Python side entirely, the
same way the pipe and snapshot moved into the host.

Two styles:

- ``walk_to(goal)`` -- query-first, called once per script loop: it advances one step toward
  the goal and returns whether the goal has been reached, so a script paces it with
  ``ctx.sleep_ticks``. It only *walks up to* transitions (doors/stairs/teleports).
- ``walk(goal)`` -- BLOCKING full walk through the native executor (``bwu_walk_run`` ->
  ``ww_executor_run``): walks the route and *executes* its transitions, returning on arrival.
  Cancel it from another thread with ``cancel``.

A route starts from the player's tile, so every method here that starts one first checks
``LocalPlayer.has_tile``. With no player, or a player with no tile, ``walk_to`` and ``walk`` return
False and ``plan``, ``walk_ex`` and ``start`` raise ``PathError``, all without reaching the host.
``NativeHost`` itself is the raw layer: it decodes no frame and has no such check.

The check reads one frame and is not a lock. ``walk``, ``walk_ex`` and ``start`` run on frames the
host reads for itself. ``walk_to`` and ``plan`` plan from the host's shared frame, which the check
has just filled, so a change reaches the host only if another thread on this host, such as a second
script on the connection, refreshes the snapshot in between.

From native-scripting-host 70f6139 on, a player gone by then is the host's refusal: ``walk`` returns
False and the others raise ``PathError``, ``walk_to`` included. A player still there with no tile
depends on the host. From b814645 on it is the same refusal, ``not in game: no tile yet``, and a
walk already started reads a tile lost later as the last one it saw. On an older host a walk fails
only if the tile is still missing when the walker reads the position, and ``walk_to`` and ``plan``
get ``PathError`` from the planner.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .actions import walk_to as _walk_action
from .entities import chebyshev
from .game import GameApi
from .snapshot_types import Tile
from .world import position_of
from ..core.native import PathError, Route
from ..core.walk import Area

# The host's text for a player with no tile, from native b814645 on. Here it also covers no player.
NO_START_TILE = "not in game: no tile yet"


class SceneWalker:
    def __init__(self, game: GameApi, host) -> None:
        self._game = game
        self._host = host  # a NativeHost: .path(x, y, plane) -> list[PathStep]

    # Every method taking a goal also takes the walk plan options, keyword-only:
    # disabled_moves (walk.moves_mask(...)), exclude (transition indices),
    # exclude_loc_siblings, and avoid (walk.Area rectangles the walker never routes through;
    # native ABI 7 and WorldWalker #44). The usual recovery: a walk_ex that failed on a
    # transition is retried with exclude=[result.fail_transition]. See
    # NativeHost.has_walk_options and NativeHost.has_avoid.

    @property
    def has_options(self) -> bool:
        """Whether the host takes walk options at all (see :meth:`options_supported`)."""
        return self._host.has_walk_options

    def options_supported(self) -> bool:
        """True when the host and its worldwalker.dll can exclude transitions."""
        return self._host.walk_options_supported()

    def plan(self, goal: Tile, *, disabled_moves: int = 0, exclude: Iterable[int] | None = None,
             exclude_loc_siblings: bool = False,
             avoid: Iterable[Area] | None = None):
        """Plan a route from the player's current tile to ``goal``. May raise ``PathError``,
        including ``not in game`` when there is no tile to route from: no player in the frame, or
        a player with no tile. Gate on ``LocalPlayer.has_tile`` to wait."""
        self._require_start_tile()
        return self._host.path(goal.x, goal.y, goal.plane, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))

    def is_reachable(self, goal: Any) -> bool:
        """Whether a route from the player's tile to ``goal`` (a tile, coordinate or entity row)
        exists: one plan, False on any :class:`PathError` (no route, not in game). The Java
        host's ``NavigationAPI.isReachable``."""
        pos = position_of(goal)
        if pos is None:
            raise TypeError(f"{goal!r} has no tile position")
        try:
            self.plan(Tile(*pos))
        except PathError:
            return False
        return True

    def route(self, goal: Tile, *, disabled_moves: int = 0, exclude: Iterable[int] | None = None,
              exclude_loc_siblings: bool = False, avoid: Iterable[Area] | None = None) -> Route:
        """Plan a route from the player's current tile to ``goal`` and return its steps with the
        transports and teleports it takes, in order (``Route.links``). Raises ``PathError`` as
        :meth:`plan`, ``WalkOptionsUnsupported`` on a worldwalker.dll without route links, and
        ``NativeUnavailable`` on a bwu_host without them."""
        self._require_start_tile()
        return self._host.path_route(goal.x, goal.y, goal.plane, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))

    def walk(self, goal: Tile, radius: int = 1, *, disabled_moves: int = 0,
             exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
             avoid: Iterable[Area] | None = None) -> bool:
        """BLOCKING full walk via the native executor: walks the route AND executes its
        transitions (doors/stairs/teleports/chains), returning True on arrival. Use this when
        the route may cross transitions ``walk_to`` can only walk up to. Cancel from another
        thread with :meth:`cancel` (e.g. the runtime on script stop). False, walking nothing,
        when there is no tile to route from: no player in the frame, or a player with no tile.
        :meth:`walk_ex` and :meth:`start` raise ``PathError`` there."""
        if not self._has_start_tile():
            return False
        return self._host.walk(goal.x, goal.y, goal.plane, radius, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))

    def cancel(self) -> None:
        """Cancel an in-flight walk: :meth:`walk` / :meth:`walk_ex` from another thread, or
        a :meth:`start` walk from anywhere."""
        self._host.walk_cancel()

    @property
    def has_progress(self) -> bool:
        """Whether the host has the walk progress surface used by the methods below."""
        return self._host.has_walk_progress

    def walk_ex(self, goal: Tile, radius: int = 1, *, disabled_moves: int = 0,
                exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
                avoid: Iterable[Area] | None = None):
        """BLOCKING like :meth:`walk`, returning a ``WalkResult``: status, the step and
        transition a failed walk failed on (``failed_on_transition``), the transition last
        attempted, re-plans used, elapsed time, and the walker's events. Raises ``PathError``,
        walking nothing, when there is no tile to route from (see :meth:`plan`)."""
        self._require_start_tile()
        return self._host.walk_ex(goal.x, goal.y, goal.plane, radius, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))

    def start(self, goal: Tile, radius: int = 1, *, disabled_moves: int = 0,
              exclude: Iterable[int] | None = None, exclude_loc_siblings: bool = False,
              avoid: Iterable[Area] | None = None) -> None:
        """Start a walk and return at once. Poll :meth:`events` / :meth:`wait` from the script
        loop and call :meth:`cancel` to step in (on ``WALK_EV_STUCK``, say) instead of waiting
        for the walker to spend its re-plans. Raises ``PathError``, starting nothing, when there
        is no tile to route from (see :meth:`plan`)."""
        self._require_start_tile()
        self._host.walk_start(goal.x, goal.y, goal.plane, radius, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))

    def status(self):
        """The current or last walk's ``WalkResult``, or None if this host never walked."""
        return self._host.walk_status()

    def wait(self, timeout_ms: int = 0):
        """Wait up to ``timeout_ms`` for the walk to end; its ``WalkResult`` (or None).
        ``timeout_ms`` is clamped to ``0..2^32-1``; ``2^32-1`` and above (``float('inf')``
        too) wait until the walk ends."""
        return self._host.walk_wait(timeout_ms)

    def events(self, since: int = 0):
        """The walk's ``WalkEvent`` records from index ``since`` on."""
        return self._host.walk_events(since)

    @property
    def has_transitions(self) -> bool:
        """Whether the host has the transition log used by :meth:`transitions` /
        :meth:`describe` (and fills ``WalkResult.transitions`` from :meth:`walk_ex`)."""
        return self._host.has_walk_transitions

    def transitions(self, since: int = 0):
        """Every ``WalkTransition`` the current or last walk attempted, from index ``since``
        on, each with its ``baked`` record."""
        return self._host.walk_transitions(since)

    def describe(self, transition_index: int):
        """The walker's baked ``TransitionInfo`` for ``transition_index``, or None for no such
        transition. May raise ``PathError`` when the walker cannot describe."""
        return self._host.walk_describe(transition_index)

    def walk_to(self, goal: Tile, radius: int = 1, step_reach: int = 12, *,
                disabled_moves: int = 0, exclude: Iterable[int] | None = None,
                exclude_loc_siblings: bool = False,
                avoid: Iterable[Area] | None = None) -> bool:
        """Advance one step toward ``goal``. Returns True once within ``radius``.

        Queues a single WALK toward the furthest walk step within ``step_reach`` tiles
        of the player (the game clamps a walk to its own reachable range), so repeated
        calls march the route. Transitions (doors, stairs) are surfaced as steps a
        caller can handle; this helper walks up to them.

        False, with nothing planned or queued, when there is no tile to route from: no player in
        the frame, or a player with no tile. Not there yet, so call it again.
        """
        me = self._game.snapshot().self_player
        if not me.has_tile:
            return False
        here = me.tile
        if chebyshev(here, goal) <= radius and here.plane == goal.plane:
            return True
        if me.moving:
            return False  # let the current walk resolve before re-issuing

        route = self._host.path(goal.x, goal.y, goal.plane, **_opts(
            disabled_moves, exclude, exclude_loc_siblings, avoid))
        target = self._next_walk_target(route, here, step_reach)
        if target is None:
            return chebyshev(here, goal) <= radius
        self._game.queue_action(_walk_action(target[0], target[1]))
        return False

    def _has_start_tile(self) -> bool:
        return self._game.snapshot().self_player.has_tile

    def _require_start_tile(self) -> None:
        if not self._has_start_tile():
            raise PathError(NO_START_TILE)

    @staticmethod
    def _next_walk_target(route, here: Tile, step_reach: int):
        """The furthest walk step within ``step_reach`` of ``here`` (else the first).

        ``route`` is the list of ``PathStep`` from ``NativeHost.path``.
        """
        chosen = None
        for step in route:
            if not step.is_walk:
                if chosen is not None:
                    break  # walk up to the transition, then stop
                continue
            if chebyshev(here, Tile(step.target_x, step.target_y, step.plane)) <= step_reach:
                chosen = (step.target_x, step.target_y)
            elif chosen is not None:
                break
        if chosen is None:
            for step in route:
                if step.is_walk:
                    return (step.target_x, step.target_y)
        return chosen


def _opts(disabled_moves: int, exclude, exclude_loc_siblings: bool, avoid=None) -> dict:
    """Only the options actually set, so a call with none keeps the host's old shape."""
    opts: dict = {}
    if avoid is not None:
        opts["avoid"] = avoid
    if disabled_moves:
        opts["disabled_moves"] = disabled_moves
    if exclude is not None:
        opts["exclude"] = exclude
    if exclude_loc_siblings:
        opts["exclude_loc_siblings"] = True
    return opts
