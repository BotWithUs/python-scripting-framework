"""``botwithus.api`` re-exports the author-facing names, and the old deep paths still work."""
import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import botwithus
import botwithus.api as api

SRC = Path(botwithus.__file__).resolve().parent.parent


def test_every_exported_name_is_the_submodules_own_object():
    assert sorted(api.__all__) == sorted(api._EXPORTS)
    for name, module in api._EXPORTS.items():
        owner = importlib.import_module(module, "botwithus.api")
        assert getattr(api, name) is getattr(owner, name), name


def test_the_common_script_imports_work_from_the_package():
    from botwithus.api import (  # noqa: F401
        ActionRefused, BotScript, ScriptContext, ServerTick, Tile, script_manifest)
    from botwithus.api.script import BotScript as deep
    assert BotScript is deep


def test_an_unknown_name_is_an_attribute_error():
    with pytest.raises(AttributeError):
        api.NotAThing  # noqa: B018


def _modules() -> list[str]:
    names = ["botwithus"]
    for info in pkgutil.walk_packages([str(SRC / "botwithus")], "botwithus."):
        if not info.name.endswith("__main__"):
            names.append(info.name)
    return names


@pytest.mark.parametrize("module", _modules())
def test_each_module_imports_first_in_a_fresh_interpreter(module):
    """No import cycle: whichever module a host or script imports first must load."""
    result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=SRC,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
