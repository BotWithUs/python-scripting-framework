"""Writable per-script settings: ``ctx.settings``.

Mirrors the Java host's ``config/ScriptSettings`` (JBotWithUsV2, phase J3): string keys and
values with typed get/put helpers. It starts from the settings in force (the GUI's config
fields) and every change the GUI commits is **merged** over it, so a key the script stored
itself survives a settings apply, as in the Java host.

**Saving.** In a GUI worker on a native host of ABI 8, :meth:`ScriptSettings.save` hands the
whole map to the GUI's settings store (it keeps it beside the run's saved config, keyed by script
path) and the next run starts from it, with the GUI's config merged over it. Declared config keys
are kept by the config itself, so a :meth:`put` on one lasts for the run only. Outside a GUI
worker (``bwu_host --py``) there is no store: :meth:`save` keeps nothing and returns False.
``ctx.config`` (the GUI's fields) is unchanged by :meth:`put`.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Callable

_TRUE = "true"


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return _TRUE if value else "false"
    return str(value)


class ScriptSettings:
    """A thread-safe string map of the script's own settings."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self._store: Callable[[dict[str, str]], bool] | None = None
        if initial:
            self._merge(initial)

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._values.get(key)

    def put(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError(f"a settings key must be a non-empty string, got {key!r}")
        with self._lock:
            self._values[key] = _text(value)

    def remove(self, key: str) -> bool:
        with self._lock:
            return self._values.pop(key, None) is not None

    def as_dict(self) -> dict[str, str]:
        with self._lock:
            return dict(self._values)

    def get_string(self, key: str, default: str = "") -> str:
        found = self.get(key)
        return default if found is None else found

    def get_int(self, key: str, default: int = 0) -> int:
        found = self.get(key)
        try:
            return default if found is None else int(found.strip())
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        found = self.get(key)
        return default if found is None else found.strip().lower() == _TRUE

    def put_int(self, key: str, value: int) -> None:
        self.put(key, int(value))

    def put_bool(self, key: str, value: bool) -> None:
        self.put(key, bool(value))

    def save(self) -> bool:
        """Write every key to the settings store. True when it was kept; False when there is no
        store (CLI mode, an older host) or it refused, with the reason logged."""
        store = self._store
        return False if store is None else bool(store(self.as_dict()))

    def _bind_store(self, store: Callable[[dict[str, str]], bool] | None) -> None:
        """Where :meth:`save` writes; installed by the GUI worker."""
        self._store = store

    def _merge(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            for key, value in values.items():
                self._values[str(key)] = _text(value)
