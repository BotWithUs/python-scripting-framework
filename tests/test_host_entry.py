"""Outside bwu_host the maintainer REPL exits at once with one line, and nothing installs a
``botwithus`` command."""
import subprocess
import sys
import tomllib
from pathlib import Path

import botwithus
from botwithus.host import __main__ as host_main

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(botwithus.__file__).resolve().parent.parent


def test_python_m_botwithus_host_fails_with_one_line_outside_the_host():
    result = subprocess.run([sys.executable, "-m", "botwithus.host", "--list"], cwd=SRC,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == host_main.NOT_IN_HOST
    assert "bwu_host" in host_main.NOT_IN_HOST


def test_pyproject_has_no_console_script_and_no_gui_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "scripts" not in project
    assert "gui" not in project.get("optional-dependencies", {})
    assert (SRC / "botwithus" / "py.typed").is_file()
