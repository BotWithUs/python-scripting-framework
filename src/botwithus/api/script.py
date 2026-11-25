"""The script-author contract: ``BotScript``, ``@script_manifest``, ``ScriptContext``.

Lifecycle: ``on_start(ctx)`` -> ``on_loop()`` (repeated) -> ``on_stop()``. ``on_loop``
returns the delay in **milliseconds** before the next call, or a negative value to stop.
Each script runs on its own thread and paces itself against ``server_tick`` via
``ctx.sleep_ticks`` -- never against ``game_cycle`` or ``publish_seq`` (see the README's
"three clocks").
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

from ..core import ctl as _ctl
from ..core.client_exit import ClientExitWatch
from .config import ConfigField
from .events import EventBus
from .game import GameApi
from .settings import ScriptSettings
from .snapshot_types import LocalPlayer
from . import timing as _timing


@dataclass(frozen=True, slots=True)
class ScriptManifest:
    name: str
    version: str = "1.0"
    author: str = ""
    description: str = ""
    category: str | None = None

    def to_wire(self) -> dict[str, str]:
        """The manifest map the GUI catalogue shows (protocol section 5.3)."""
        out = {"name": self.name, "version": self.version, "author": self.author,
               "description": self.description}
        if self.category is not None:
            out["category"] = self.category
        return out


def script_manifest(name: str, version: str = "1.0", author: str = "",
                    description: str = "", category: str | None = None) -> Callable[[type], type]:
    """Attach a ``ScriptManifest`` to a ``BotScript`` subclass (read by the loader).

    ``author`` defaults to ``""``, which the GUI shows as "Unknown author", as the Java host
    does. ``category`` groups the script in the GUI; use one of the Java host's category names
    (``Skilling``, ``Combat``, ``Woodcutting``, ... ``Other``). Left out, it is
    "Uncategorized"."""

    def _decorate(cls: type) -> type:
        cls.__bwu_manifest__ = ScriptManifest(name, version, author, description, category)
        return cls

    return _decorate


def manifest_of(cls: type) -> ScriptManifest:
    """Return the class's manifest, defaulting to its class name if undecorated."""
    m = getattr(cls, "__bwu_manifest__", None)
    if isinstance(m, ScriptManifest):
        return m
    return ScriptManifest(name=cls.__name__)


class ScriptLog:
    """``ctx.log``: the script's own logger, better than ``print``.

    ``debug``/``info``/``warn``/``error`` (and ``warning``/``exception``) take the usual
    ``logging`` arguments. Under ``bwu_host`` every record from the script's thread is written
    to the script's run log at DEBUG and up, redacted, whatever the console shows; ``print``
    and the standard ``logging`` module land there too.
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(f"script.{name}")

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None:
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:
        self._logger.info(msg, *args, **kwargs)

    def warn(self, msg: object, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(msg, *args, **kwargs)

    warning = warn

    def error(self, msg: object, *args: Any, **kwargs: Any) -> None:
        self._logger.error(msg, *args, **kwargs)

    def exception(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """An ERROR record with the current exception's traceback (call it in ``except``)."""
        self._logger.exception(msg, *args, **kwargs)


# How often ``ScriptContext.sleep`` looks for a client exit while it waits. Each look is one
# ``refresh`` while the client is up, so a sleeping script costs ten cheap calls a second.
EXIT_POLL_S = 0.1


class ScriptContext:
    """Everything a script is handed at start: the API, events, logging and cooperative waits.

    Constructed by the runtime. ``navigation`` is a ``SceneWalker`` over the native host's
    path surface (``bwu_path``); it is ``None`` only when navigation is disabled. ``log`` is
    the script's :class:`ScriptLog`.
    """

    def __init__(self, name: str, game: GameApi, events: EventBus,
                 stop_event: threading.Event) -> None:
        self.name = name
        self.game = game
        self.events = events
        self.log = ScriptLog(name)
        self._stop = stop_event
        self.navigation: Any = None  # SceneWalker | None, injected by the runtime
        # The script's plugin UI (botwithus.api.ui.PluginUi), injected by the Connection. None
        # for a context with no client behind it.
        self.ui: Any = None
        self._config: Mapping[str, Any] = MappingProxyType({})
        # Pause switch (J3 ScriptPause): set = running, clear = paused. The runner holds before
        # each on_loop while paused; the condition waits (botwithus.api.timing) hold too.
        self._resumed = threading.Event()
        self._resumed.set()
        # The host's run control (ABI 8), installed by the GUI worker: told of every pause the
        # script makes, so the GUI shows it. One lock orders a script pause against a GUI one.
        self._pause_lock = threading.Lock()
        self._pause_sink: Callable[[bool], Any] | None = None
        self.settings = ScriptSettings()
        # The client-gone rule for this context's client, injected by the Connection. None
        # (a context with no client behind it): the waits never see an exit.
        self._exit_watch: ClientExitWatch | None = None

    # --- config --------------------------------------------------------------

    @property
    def config(self) -> Mapping[str, Any]:
        """The settings in force: a read-only mapping of every config field's key to its
        value. Replaced, never mutated, before each ``on_config_update``."""
        return self._config

    def _set_config(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Install a new full set; returns the plain-dict copy handed to the script."""
        plain = dict(values)
        self._config = MappingProxyType(dict(plain))
        self.settings._merge(plain)
        return plain

    def _close_ui_delivery(self) -> None:
        """No ui event reaches this script any more; waits for one in flight. Run by the
        framework when the script stops, before ``on_stop``."""
        if self.ui is not None:
            self.ui.close_delivery()

    def _delivering_ui_here(self) -> bool:
        """True while this thread is running one of this script's ui handlers."""
        return self.ui is not None and self.ui.delivering_here()

    def _release_ui(self) -> None:
        """Destroy the components this script made (``ui.destroy`` each; never another
        script's). Run by the framework once the script has stopped, after ``on_stop``."""
        if self.ui is not None:
            self.ui.release()

    # --- convenience ---------------------------------------------------------

    def snapshot(self):
        return self.game.snapshot()

    @property
    def self_player(self) -> LocalPlayer:
        return self.game.snapshot().self_player

    # --- cooperative control -------------------------------------------------

    def is_stop_requested(self) -> bool:
        """True once the script should stop: ``stop_self()``, the runner's stop, or (in a GUI
        worker) the GUI's stop."""
        return self._stop.is_set() or _ctl.stopping()

    @property
    def stopping(self) -> bool:
        """The same as :meth:`is_stop_requested`, as a property."""
        return self.is_stop_requested()

    def stop_self(self) -> None:
        self._stop.set()

    # --- pause / resume (JBotWithUsV2 J3 ScriptPause) -------------------------

    def pause(self) -> None:
        """Pause the script: the runner calls no further ``on_loop`` and the condition waits
        hold, without spending their timeout or tick budget, until :meth:`resume`. A stop still
        ends a paused script. In a GUI worker the GUI sees it, and can resume it."""
        with self._pause_lock:
            if self._pause_sink is not None:
                self._pause_sink(True)
            self._resumed.clear()

    def resume(self) -> None:
        with self._pause_lock:
            if self._pause_sink is not None:
                self._pause_sink(False)
            self._resumed.set()

    def _apply_pause(self, paused: bool) -> None:
        """The host's switch changed (the GUI paused or resumed the run): follow it, without
        telling the host back."""
        with self._pause_lock:
            if paused:
                self._resumed.clear()
            else:
                self._resumed.set()

    def _bind_pause_sink(self, sink: Callable[[bool], Any] | None) -> None:
        self._pause_sink = sink

    @property
    def is_paused(self) -> bool:
        return not self._resumed.is_set()

    def _await_resumed(self) -> bool:
        """Block while paused. False when a stop came (or had come) first."""
        while self.is_paused:
            if self.is_stop_requested() or self._client_exited():
                return False
            self._resumed.wait(EXIT_POLL_S)
        return not self.is_stop_requested()

    # --- condition waits (botwithus.api.timing, bound to this context) --------

    def delay_until(self, condition: Callable[[], bool], timeout_ms: int) -> bool:
        """:func:`botwithus.api.timing.delay_until` for this script."""
        return _timing.delay_until(condition, timeout_ms, ctx=self)

    def delay_while(self, condition: Callable[[], bool], timeout_ms: int) -> bool:
        return _timing.delay_while(condition, timeout_ms, ctx=self)

    def delay_until_ticks(self, condition: Callable[[], bool], max_ticks: int) -> bool:
        """Poll ``condition`` for up to ``max_ticks`` server ticks."""
        return _timing.delay_until_ticks(self.game, condition, max_ticks, ctx=self)

    def delay_ticks(self, min_ticks: int, max_ticks: int | None = None) -> bool:
        """Wait a random ``min_ticks..max_ticks`` server ticks."""
        return _timing.delay_ticks(self.game, min_ticks, max_ticks, ctx=self)

    def _client_exited(self) -> bool:
        """One non-blocking look at the client-gone rule (``core.client_exit``)."""
        return self._exit_watch is not None and self._exit_watch.look()

    def sleep(self, ms: int) -> bool:
        """Sleep up to ``ms`` milliseconds, waking early if a stop is requested or the client
        exits.

        Returns True if the full time elapsed, False if a stop came first (or had already
        come) or the client exited. In a GUI worker the GUI's stop wakes it within 250 ms; a
        client exit ends it about :data:`~botwithus.core.client_exit.CLIENT_GONE_CONFIRM_S`
        after the exit.
        """
        if self.is_stop_requested():
            return False
        if ms <= 0:
            return True
        # An infinite delay waits for the stop or the exit (the deadline stays inf).
        deadline = time.monotonic() + ms / 1000.0
        while (left := deadline - time.monotonic()) > 0:
            if self._stop.wait(min(left, EXIT_POLL_S)) or _ctl.stopping():
                return False
            if self._client_exited():
                return False
        return True

    def sleep_ticks(self, ticks: int = 1, timeout_ms: int = 6000, poll_ms: int = 60) -> bool:
        """Wait for ``server_tick`` to advance by ``ticks`` (the correct pacing clock).

        Returns True on advance, False on stop, timeout, or the client exiting. After an exit
        the reads keep returning the last frame, so the tick never moves; the wait returns
        False about :data:`~botwithus.core.client_exit.CLIENT_GONE_CONFIRM_S` after the exit
        instead of sitting out ``timeout_ms``. Never paces off game_cycle or publish_seq.
        """
        if self.is_stop_requested():
            return False
        start = self.game.snapshot().server_tick
        waited = 0
        while waited < timeout_ms:
            if self._stop.wait(poll_ms / 1000.0) or _ctl.stopping():
                return False
            if self._client_exited():
                return False
            waited += poll_ms
            now = self.game.snapshot().server_tick
            if now >= 0 and start >= 0 and now - start >= ticks:
                return True
            if start < 0:  # tick wasn't established yet; re-baseline once it is
                start = now
        return False


class BotScript(ABC):
    """Base class for a single-client script. Discovered by type-scan."""

    def on_start(self, ctx: ScriptContext) -> None:
        """Called once when the script starts. Cache references from ``ctx``."""

    @abstractmethod
    def on_loop(self) -> int:
        """Called repeatedly. Return ms until the next call, or a negative value to stop."""
        raise NotImplementedError

    def on_stop(self) -> None:
        """Called once when the script stops."""

    # --- config (optional) ---------------------------------------------------

    def get_config_fields(self) -> list[ConfigField | dict]:
        """The script's settings, shown in the GUI. Build them with the helpers in
        :mod:`botwithus.api.config` (``int_field``, ``string_field``, ``bool_field``,
        ``choice_field``, ``item_id_field``) or as plain dicts with the same keys. The rules
        are checked when the script is described or started."""
        return []

    def on_config_update(self, config: dict) -> None:
        """Receives the full set of settings in force (every field's key): once before
        ``on_start``, then after each change the user commits, on the script's thread between
        ``on_loop`` calls. ``ctx.config`` holds the same values. Raising here ends the run as
        a crash."""
