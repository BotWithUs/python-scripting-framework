"""The README quickstart script runs as written: its class and its ``__main__`` block, as
``bwu_host --py`` would run the file, against the fake native backend (no client, no host)."""
import re
from pathlib import Path

from botwithus.core import native as native_mod

from fakes import FakeNative, build_snapshot

README = Path(__file__).resolve().parent.parent / "README.md"
# The script's only pacing delay, cut so ten loops take milliseconds, not six seconds.
LOOP_DELAY = "else 600"


def _quickstart_source() -> str:
    text = README.read_text(encoding="utf-8")
    section = text.split("### The shape of a script", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    assert match, "the quickstart has no python block"
    return match.group(1)


def test_the_quickstart_script_runs_to_its_own_stop():
    source = _quickstart_source()
    assert LOOP_DELAY in source
    backend = FakeNative(snapshot=build_snapshot(self_tile=(3200, 3201, 0)))
    native_mod.set_backend(backend)
    scope = {"__name__": "__main__"}
    exec(compile(source.replace(LOOP_DELAY, "else 1"), str(README), "exec"), scope)
    conn = scope["conn"]
    runner = conn._runners["Hello"]
    # Stopped by its own -1 after ten loops, not by an error (which also empties the list).
    # close() stops a finished runner, which leaves it STOPPED; ERROR is what a crash leaves.
    assert runner.state.name == "STOPPED", runner.state
    assert runner._script.loops == 10
    assert conn.running_scripts() == []
    assert backend.detached          # conn.close() ran


def test_the_quickstart_exits_cleanly_with_no_client():
    class NoClients(FakeNative):
        def discover_pids(self):
            return []
    native_mod.set_backend(NoClients())
    scope = {"__name__": "__main__"}
    try:
        exec(compile(_quickstart_source(), str(README), "exec"), scope)
    except SystemExit as exc:
        assert str(exc) == "no game client found"
    else:
        raise AssertionError("expected SystemExit")


def _settings_source() -> str:
    text = README.read_text(encoding="utf-8")
    section = text.split("### Settings", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    assert match, "the settings section has no python block"
    return match.group(1)


def test_the_settings_example_is_a_valid_schema():
    from botwithus.api import config as C
    scope = {"__name__": "readme_settings"}
    exec(compile(_settings_source(), str(README), "exec"), scope)
    script = scope["Cutter"]()
    schema = C.schema_to_wire(script.get_config_fields())
    C.validate_schema(schema)
    script.on_config_update(C.defaults(schema))
    assert (script.logs, script.tree) == (28, "Willow")


def test_the_example_scripts_describe_cleanly(tmp_path, monkeypatch):
    """Both shipped examples load in plugin mode without attaching or launching."""
    import sys
    from botwithus import worker
    from fakes import FakeRunlog
    examples = README.parent / "examples"
    for path, plugin in ((examples / "example_script" / "example_script.py", True),
                         (examples / "manager_script" / "manager.py", False)):
        monkeypatch.setattr(sys, "path", list(sys.path))
        fake = FakeRunlog(ctl_mode=FakeRunlog.CTL_DESCRIBE)
        native_mod.set_backend(fake)
        worker.describe(str(path))
        sys.modules.pop("bwu_script", None)
        assert fake.crashes == [], fake.crashes
        desc = fake.ctl_log[0][1]
        assert desc.get("self_running", False) is not plugin
        from botwithus.api import config as C
        C.validate_describe(desc)
