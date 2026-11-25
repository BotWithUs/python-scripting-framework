"""Script discovery/loading behind a pluggable artifact abstraction.

The loader is swappable; the lifecycle is not. Three artifact shapes load through the
same ``ScriptSource`` interface and yield the same ``LoadedScript`` records, so a script
lands in the same registry whether it came from a local directory, an entitlement-gated
bundle, or a native-compiled module:

- ``LocalScriptSource``  -- plain ``.py`` files (dev / fully trusted). Implemented.
- ``SecureScriptSource`` -- entitlement-gated encrypted bundle. **Stub** (the SDN seam):
  this is where a launcher-distributed closed decryptor plugs in; crypto/keys never live
  in this open framework.
- ``NativeScriptSource`` -- a compiled ``.pyd`` (Nuitka/Cython). **Stub.**

Only ``LocalScriptSource`` is wired for v1; the other two exist so nothing in the core
hardcodes "scripts are ``.py`` files".
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..api.script import BotScript, ScriptManifest, manifest_of

_log = logging.getLogger("botwithus.loader")


def default_scripts_dir() -> Path:
    """``~/.botwithus/scripts/python``, beside the Lua frontend's ``scripts/lua``;
    ``BWU_PY_SCRIPTS_DIR`` overrides it."""
    env = os.environ.get("BWU_PY_SCRIPTS_DIR")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".botwithus" / "scripts" / "python"


@dataclass(frozen=True, slots=True)
class LoadedScript:
    manifest: ScriptManifest
    factory: Callable[[], BotScript]
    origin: str  # human-readable source (path, bundle id, ...)

    def create(self) -> BotScript:
        return self.factory()


class ScriptSource(Protocol):
    def discover(self) -> list[LoadedScript]:
        ...


def _scripts_in_module(module, origin: str) -> list[LoadedScript]:
    found: list[LoadedScript] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (issubclass(obj, BotScript) and obj is not BotScript
                and obj.__module__ == module.__name__
                and not inspect.isabstract(obj)):
            found.append(LoadedScript(manifest_of(obj), obj, origin))
    return found


class LocalScriptSource:
    """Discover ``BotScript`` subclasses in every ``.py`` under a directory."""

    def __init__(self, scripts_dir: Path | None = None) -> None:
        self.scripts_dir = scripts_dir or default_scripts_dir()

    def discover(self) -> list[LoadedScript]:
        out: list[LoadedScript] = []
        if not self.scripts_dir.is_dir():
            _log.info("scripts dir %s does not exist; no local scripts", self.scripts_dir)
            return out
        for path in sorted(self.scripts_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = self._load_module(path)
            if module is not None:
                out.extend(_scripts_in_module(module, str(path)))
        return out

    @staticmethod
    def _load_module(path: Path):
        mod_name = f"bwu_script_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module  # allow reload by re-import
            spec.loader.exec_module(module)
            return module
        except Exception:
            _log.exception("failed to load script file %s", path)
            return None


class SecureScriptSource:
    """SDN entitlement-gated bundle source. Not implemented in v1 (the SDN seam)."""

    def discover(self) -> list[LoadedScript]:
        raise NotImplementedError(
            "SecureScriptSource requires the launcher-distributed decryptor. "
            "No crypto lives in the open framework."
        )


class NativeScriptSource:
    """Compiled ``.pyd`` source (Nuitka/Cython). Not implemented in v1."""

    def discover(self) -> list[LoadedScript]:
        raise NotImplementedError("NativeScriptSource is a planned protection option.")


class ScriptLoader:
    """Aggregate discovery over one or more sources."""

    def __init__(self, sources: list[ScriptSource] | None = None) -> None:
        self._sources = sources or [LocalScriptSource()]

    def discover(self) -> list[LoadedScript]:
        scripts: list[LoadedScript] = []
        for source in self._sources:
            try:
                scripts.extend(source.discover())
            except NotImplementedError:
                continue  # an inert source (stubbed) contributes nothing
            except Exception:
                _log.exception("script source %s failed", type(source).__name__)
        return scripts

    def by_name(self, name: str) -> LoadedScript | None:
        for s in self.discover():
            if s.manifest.name == name:
                return s
        return None
