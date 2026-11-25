"""The client exiting while a script waits or calls: ``ctx.sleep_ticks`` / ``ctx.sleep`` end
about one confirm time after the exit (the reads keep returning the last frame, so the tick
never moves), a pure tick stall still times out as before, and an RPC after the exit
("client exited") marks the host dead and ends the run stopped, in a GUI worker and under the
CLI runner alike."""
from __future__ import annotations

import threading
import time

import pytest

from botwithus import worker
from botwithus.api import BotScript
from botwithus.core import client_exit
from botwithus.core import native as native_mod
from botwithus.core.client_exit import ClientExitWatch
from botwithus.core.connection import Connection
from botwithus.core.native import ClientExited, NativeHost
from botwithus.core.rpc import RpcError
from botwithus.core import runtime as runtime_mod
from botwithus.core.runtime import ScriptState
from botwithus.host.manager import ConnectionManager

from fakes import FakeNative
from test_worker import PID, Recorder, _names, _script, env  # noqa: F401 - env is a fixture
from test_worker_review import ExitingClient, _install

# The bound: a wait ends within about this long of the client exiting, at the real
# one-second confirm time.
EXIT_LATENCY_S = 1.5
# Longer than the whole test: only the exit (or the stall timeout under test) ends a wait.
LONG_MS = 30_000
JOIN_S = 5.0
# The stall tests' 300 ms timeout, less Windows timer slack (Event.wait can return a hair early).
STALL_MIN_S = 0.25
CLIENT_EXITED = "client exited"


class RpcAfterExit(ExitingClient):
    """After the exit every RPC fails the way the native host's does."""

    def rpc_raw(self, handle, req):
        if self.exited:
            raise RuntimeError(f"rpc failed: {CLIENT_EXITED}")
        return super().rpc_raw(handle, req)


def _exiting(env, cls=ExitingClient):
    return _install(env, cls(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))


# --- a wait in the middle of on_loop ---------------------------------------------------------

WAITS_THEN_EXITS = """
    import time
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        n = 0
        def on_start(self, ctx): S.ctx = ctx
        def on_loop(self):
            S.n += 1
            if S.n >= 3:
                return -1                  # a backstop if the exit went unseen
            T.fake.exited = True
            exited_at = time.monotonic()
            result = WAIT
            T.events.append(("waited", result, time.monotonic() - exited_at))
            return 0
        def on_stop(self):
            T.events.append(("on_stop",))
"""


def _waited(env):
    return [e for e in env.trace.events if e[0] == "waited"]


# The longest a bounded run may take before the test fails instead of hanging (a wait that
# never sees the exit -- sleep(inf) -- would otherwise block the suite forever).
RUN_BOUND_S = 10.0


def _run_bounded(fake, path):
    """``worker.run`` on a thread, failing (not hanging) if it outlives RUN_BOUND_S. On a
    timeout the GUI's stop ends the run so the thread does not leak into later tests."""
    done = threading.Thread(target=worker.run, args=(path, PID), daemon=True)
    done.start()
    done.join(RUN_BOUND_S)
    if done.is_alive():
        fake.gui_stop()
        done.join(RUN_BOUND_S)
        pytest.fail(f"worker.run still running after {RUN_BOUND_S} s: the exit went unseen")


@pytest.mark.parametrize("wait", [
    f"S.ctx.sleep_ticks(2, timeout_ms={LONG_MS})",
    "S.ctx.sleep_ticks(2)",                       # the default 6 s timeout
    f"S.ctx.sleep({LONG_MS})",
    "S.ctx.sleep(float('inf'))",
])
def test_a_wait_returns_false_soon_after_the_client_exits(env, wait):
    """At the real confirm time: False within EXIT_LATENCY_S, then the loop's own check
    ends the run stopped, without another on_loop."""
    assert client_exit.CLIENT_GONE_CONFIRM_S == 1.0
    fake = _exiting(env)
    started = time.monotonic()
    _run_bounded(fake, _script(env, WAITS_THEN_EXITS.replace("WAIT", wait)))
    [(_, result, latency)] = _waited(env)
    assert result is False
    assert client_exit.CLIENT_GONE_CONFIRM_S <= latency < EXIT_LATENCY_S
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]
    assert time.monotonic() - started < EXIT_LATENCY_S + 0.5   # no second confirm wait
    assert fake.crashes == []


def test_a_delay_returned_from_on_loop_ends_soon_after_the_client_exits(env):
    fake = _exiting(env)
    timer = threading.Timer(0.2, lambda: setattr(fake, "exited", True))
    timer.start()
    started = time.monotonic()
    _run_bounded(fake, _script(env, f"""
        from botwithus.api import BotScript
        class S(BotScript):
            def on_loop(self): return {LONG_MS}
    """))
    timer.join()
    assert time.monotonic() - started < 0.2 + EXIT_LATENCY_S
    assert fake.ctl_states()[-1] == "stopping"
    assert fake.crashes == []


# --- a pure tick stall ------------------------------------------------------------------------

class CountsDiscovery(Recorder):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.discoveries = 0

    def discover_pids(self):
        self.discoveries += 1
        return super().discover_pids()


STALLS = """
    import time
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        n = 0
        def on_start(self, ctx): S.ctx = ctx
        def on_loop(self):
            S.n += 1
            began = time.monotonic()
            result = S.ctx.sleep_ticks(1, timeout_ms=300)
            T.events.append(("waited", result, time.monotonic() - began))
            return -1 if S.n >= 2 else 0
"""


def test_a_tick_stall_with_the_client_up_still_times_out(env):
    """The fake's tick never moves and the client is up: the wait sits out its timeout, the
    run goes on to the next on_loop, and the poll never asked discovery (refresh held)."""
    fake = _install(env, CountsDiscovery(env.trace.events, ctl_mode=FakeNative.CTL_WORKER))
    worker.run(_script(env, STALLS), PID)
    waited = _waited(env)
    assert [w[1] for w in waited] == [False, False]
    assert all(w[2] >= STALL_MIN_S for w in waited)
    assert fake.discoveries == 0
    assert fake.crashes == []


def test_a_stall_with_refresh_failing_but_the_client_listed_still_times_out(env, monkeypatch):
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", 0.05)

    class Listed(ExitingClient):
        exited = True

        def discover_pids(self):
            return Recorder.discover_pids(self)

    _exiting(env, Listed)
    worker.run(_script(env, STALLS), PID)
    assert [w[1] for w in _waited(env)] == [False, False]
    assert all(w[2] >= STALL_MIN_S for w in _waited(env))


# --- an RPC after the exit --------------------------------------------------------------------

CALLS_AFTER_EXIT = """
    import worker_trace as T
    from botwithus.api import BotScript
    class S(BotScript):
        n = 0
        def on_start(self, ctx): S.ctx = ctx
        def on_loop(self):
            S.n += 1
            if S.n >= 3:
                return -1
            T.fake.exited = True
            try:
                S.ctx.game.login_state()
            except BaseException as exc:
                T.events.append(("raised", type(exc).__name__))
                raise
            return 0
        def on_stop(self):
            T.events.append(("on_stop",))
            S.ctx.game.login_state()     # fails too; still not a crash
"""


def test_an_rpc_after_the_exit_marks_the_host_dead_and_ends_the_run_stopped(env, monkeypatch):
    """No confirm wait (it is 30 s here): the dead host is gone at once."""
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", 30.0)
    fake = _exiting(env, RpcAfterExit)
    started = time.monotonic()
    worker.run(_script(env, CALLS_AFTER_EXIT), PID)
    assert ("raised", "ClientExited") in env.trace.events
    assert _names(env.trace.events)[-4:] == ["raised", "state:stopping", "on_stop", "detach"]
    assert time.monotonic() - started < 1.0
    assert fake.crashes == []


def _host_after_exit(message: str) -> NativeHost:
    native_mod.set_backend(FakeNative(responder=lambda m, p: f"__error__:{message}"))
    return NativeHost.attach(PID)


def test_client_exited_is_a_transport_death():
    host = _host_after_exit(CLIENT_EXITED)
    with pytest.raises(ClientExited, match=CLIENT_EXITED):
        host.rpc("get_login_state")
    assert not host.alive
    assert issubclass(ClientExited, ConnectionError)


def test_a_handler_error_leaves_the_host_alive():
    host = _host_after_exit("bad_param")
    with pytest.raises(RpcError):
        host.rpc("get_login_state")
    assert host.alive


def test_another_transport_death_is_not_client_exited():
    host = _host_after_exit("pipe disconnected")
    with pytest.raises(ConnectionError) as info:
        host.rpc("get_login_state")
    assert type(info.value) is ConnectionError
    assert not host.alive


# --- the CLI runner ---------------------------------------------------------------------------

class CallsAfterExit(BotScript):
    def __init__(self, fake):
        self.fake = fake
        self.raised = []

    def on_start(self, ctx):
        self.ctx = ctx

    def on_loop(self):
        self.fake.exited = True
        try:
            self.ctx.game.login_state()
        except BaseException as exc:
            self.raised.append(type(exc).__name__)
            raise
        return 0

    def on_stop(self):
        self.ctx.game.login_state()


class SleepsThroughExit(BotScript):
    def __init__(self, fake):
        self.fake = fake
        self.loops = 0

    def on_loop(self):
        self.loops += 1
        self.fake.exited = True
        return LONG_MS


def _cli_fake(cls):
    class Fake(cls):
        pass
    fake = Fake([])
    native_mod.set_backend(fake)
    return fake


@pytest.fixture
def cli_backend():
    yield
    native_mod.set_backend(None)


def test_the_cli_runner_ends_stopped_after_an_rpc_fails_on_exit(cli_backend):
    fake = _cli_fake(RpcAfterExit)
    conn = Connection(PID, enable_navigation=False)
    script = CallsAfterExit(fake)
    conn.start_script(script)
    runner = conn._runners["CallsAfterExit"]
    runner._thread.join(JOIN_S)
    assert not runner._thread.is_alive()
    assert script.raised == ["ClientExited"]
    assert runner.state is ScriptState.STOPPED
    assert not conn.alive               # the manager's sweep detaches it
    assert fake.crashes == []
    conn.close()


def test_the_cli_runner_ends_a_sleep_soon_after_the_client_exits(cli_backend):
    fake = _cli_fake(ExitingClient)
    conn = Connection(PID, enable_navigation=False)
    script = SleepsThroughExit(fake)
    started = time.monotonic()
    conn.start_script(script)
    runner = conn._runners["SleepsThroughExit"]
    runner._thread.join(JOIN_S)
    assert not runner._thread.is_alive()
    assert time.monotonic() - started < EXIT_LATENCY_S
    assert script.loops == 1
    assert runner.state is ScriptState.STOPPED
    assert fake.crashes == []
    conn.close()


# --- the rule itself --------------------------------------------------------------------------

class _Conn:
    def __init__(self):
        self.alive = True
        self.readable = True
        self.listed = True
        self.listed_asks = 0

    def snapshot_readable(self):
        return self.readable

    def client_listed(self):
        self.listed_asks += 1
        return self.listed


# The rule tests' confirm time. Wide enough that Windows sleep overshoot (a timer tick or
# two) cannot push a 'pair' sleep past the stale cut-off at twice this.
QUICK_CONFIRM_S = 0.2
# Lands between one and two confirm times: a pair, never stale.
PAIR_SLEEP_S = 1.25 * QUICK_CONFIRM_S
# Past two confirm times: the remembered miss is stale.
STALE_SLEEP_S = (client_exit.FIRST_MISS_STALE_FACTOR + 0.5) * QUICK_CONFIRM_S


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", QUICK_CONFIRM_S)


def test_look_needs_two_misses_the_confirm_time_apart(quick):
    conn = _Conn()
    watch = ClientExitWatch(conn)
    conn.readable = conn.listed = False
    assert not watch.look()            # first miss
    assert not watch.look()            # too soon
    time.sleep(PAIR_SLEEP_S)
    assert watch.look()
    assert watch.look()                # stays gone


def test_a_reachable_look_forgets_the_first_miss(quick):
    conn = _Conn()
    watch = ClientExitWatch(conn)
    conn.readable = conn.listed = False
    watch.look()
    time.sleep(PAIR_SLEEP_S)
    conn.listed = True
    assert not watch.look()
    conn.listed = False
    assert not watch.look()            # a fresh first miss, not the old one


def test_discovery_is_asked_only_after_refresh_fails():
    conn = _Conn()
    watch = ClientExitWatch(conn)
    for _ in range(5):
        assert not watch.look()
    assert conn.listed_asks == 0


def test_a_dead_connection_is_gone_at_once():
    conn = _Conn()
    conn.alive = False
    assert ClientExitWatch(conn).look()


def test_confirm_waits_out_the_rest_and_looks_again(quick):
    conn = _Conn()
    conn.readable = conn.listed = False
    started = time.monotonic()
    assert ClientExitWatch(conn).confirm(threading.Event())
    assert time.monotonic() - started >= QUICK_CONFIRM_S


def test_a_context_with_no_client_never_sees_an_exit(env):
    """A ScriptContext built without a Connection (no watch) sleeps as before."""
    from botwithus.api.script import ScriptContext
    ctx = ScriptContext("x", None, None, threading.Event())
    started = time.monotonic()
    assert ctx.sleep(150) is True
    assert time.monotonic() - started >= 0.15





# --- the stale first miss (review nit) -------------------------------------------------------

def test_a_first_miss_older_than_twice_the_confirm_time_is_dropped(quick):
    """A miss long ago (no look since) cannot pair with a new one: the new miss starts over."""
    conn = _Conn()
    watch = ClientExitWatch(conn)
    conn.readable = conn.listed = False
    assert not watch.look()                                       # the old first miss
    time.sleep(STALE_SLEEP_S)                                     # past 2x confirm
    assert not watch.look()                                       # stale: a fresh first miss
    time.sleep(PAIR_SLEEP_S)
    assert watch.look()                                           # paired with the fresh one


class _Oversleeps:
    """A stop event whose wait sleeps far past its timeout: a loaded machine."""

    def wait(self, timeout):
        time.sleep(timeout * (client_exit.FIRST_MISS_STALE_FACTOR + 1))
        return False


def test_confirm_survives_an_oversleep_past_the_stale_cut_off(quick):
    """confirm's second look pairs with its own first miss however late the wait wakes."""
    conn = _Conn()
    conn.readable = conn.listed = False
    assert ClientExitWatch(conn).confirm(_Oversleeps())


# --- ClientExited from a config callback (review nit) ----------------------------------------

CONFIG_AFTER_EXIT = """
    import worker_trace as T
    from botwithus.api import BotScript, int_field
    class S(BotScript):
        n = 0
        def get_config_fields(self): return [int_field("n", "N", 1)]
        def on_start(self, ctx): S.ctx = ctx
        def on_config_update(self, config):
            T.events.append(("config", dict(config)))
            if config["n"] == 9:
                T.fake.exited = True
                S.ctx.game.login_state()       # raises ClientExited
        def on_loop(self):
            S.n += 1
            if S.n == 1:
                T.fake.gui_set_config(7, {"n": 9})
            return -1 if S.n >= 3 else 0
        def on_stop(self): T.events.append(("on_stop",))
"""


def test_client_exited_from_a_queued_config_update_ends_the_run_stopped(env, monkeypatch):
    monkeypatch.setattr(client_exit, "CLIENT_GONE_CONFIRM_S", 30.0)
    fake = _exiting(env, RpcAfterExit)
    _run_bounded(fake, _script(env, CONFIG_AFTER_EXIT))
    done = [e for e in fake.ctl_log if e[0] == "done"]
    assert [(d[1], d[2]) for d in done] == [(7, False)]        # refused, not applied
    assert done[0][3].startswith("client exited")
    assert _names(env.trace.events)[-3:] == ["state:stopping", "on_stop", "detach"]
    assert fake.crashes == []


class ConfigAfterExit(BotScript):
    def __init__(self):
        self.started = False

    def on_start(self, ctx):
        self.started = True

    def on_config_update(self, config):
        raise ClientExited("rpc failed: client exited")

    def on_loop(self):
        return -1


def test_client_exited_from_the_cli_initial_config_update_ends_stopped(cli_backend):
    fake = _cli_fake(ExitingClient)
    conn = Connection(PID, enable_navigation=False)
    script = ConfigAfterExit()
    conn.start_script(script)
    runner = conn._runners["ConfigAfterExit"]
    runner._thread.join(JOIN_S)
    assert not runner._thread.is_alive()
    assert runner.state is ScriptState.STOPPED
    assert not script.started
    assert fake.crashes == []
    conn.close()


# --- no use after free on detach (review should-fix) -----------------------------------------

# Every NativeHost method that hands the native handle down, with arguments.
HANDLE_CALLS = [
    ("refresh", ()), ("snapshot_bytes", ()), ("rpc", ("get_login_state",)),
    ("read_varps", ([1],)), ("read_varbits", ([1],)), ("walk_options_supported", ()),
    ("path", (1, 2)), ("walk", (1, 2)), ("walk_ex", (1, 2)), ("walk_start", (1, 2)),
    ("walk_status", ()), ("walk_wait", (0,)), ("walk_events", ()),
    ("walk_transitions", ()), ("walk_describe", (0,)), ("poll_events_raw", ()),
]
# The backend functions those methods call.
BACKEND_FNS = ["refresh", "snapshot_bytes", "rpc_raw", "read_varps", "read_varbits",
               "walk_options_supported", "path", "walk", "walk_ex", "walk_start",
               "walk_status", "walk_wait", "walk_events", "walk_transitions", "walk_describe",
               "poll_events", "walk_cancel"]


def _detach_counter(fake):
    fake.detach_calls = []
    fake.detach = lambda h: fake.detach_calls.append(h)


@pytest.fixture
def spied_host(cli_backend):
    """An attached NativeHost whose backend records every call that is passed a handle."""
    fake = FakeNative()
    native_mod.set_backend(fake)
    host = NativeHost.attach(PID)
    touched = []
    for name in BACKEND_FNS:
        setattr(fake, name, lambda *a, _n=name, **k: touched.append((_n, a[0] if a else None)))
    _detach_counter(fake)
    return host, fake, touched


@pytest.mark.parametrize("method,args", HANDLE_CALLS, ids=[m for m, _ in HANDLE_CALLS])
def test_every_handle_call_after_detach_raises_and_passes_nothing_down(spied_host, method, args):
    host, fake, touched = spied_host
    assert host.detach() is True
    assert host._h is None
    with pytest.raises(native_mod.HostDetached, match="detached"):
        getattr(host, method)(*args)
    assert touched == []
    assert issubclass(native_mod.HostDetached, ConnectionError)


def test_walk_cancel_after_detach_is_a_no_op(spied_host):
    host, fake, touched = spied_host
    host.detach()
    host.walk_cancel()
    assert touched == []


def test_detach_frees_the_handle_once(spied_host):
    host, fake, _ = spied_host
    handle = host._h
    assert host.detach() is True
    assert host.detach() is True
    assert fake.detach_calls == [handle]


def _blocking_poll(fake, release):
    entered = threading.Event()

    def poll(h):
        entered.set()
        release.wait(JOIN_S)
        return []
    fake.poll_events = poll
    return entered


def test_detach_waits_for_a_call_in_flight_before_freeing(spied_host):
    host, fake, _ = spied_host
    release = threading.Event()
    entered = _blocking_poll(fake, release)
    caller = threading.Thread(target=host.poll_events_raw)
    caller.start()
    assert entered.wait(JOIN_S)
    detacher = threading.Thread(target=host.detach)
    detacher.start()
    time.sleep(0.2)
    assert fake.detach_calls == []              # not freed under the live call
    with pytest.raises(native_mod.HostDetached):
        host.refresh()                          # new calls are refused at once
    release.set()
    caller.join(JOIN_S)
    detacher.join(JOIN_S)
    assert len(fake.detach_calls) == 1


def test_detach_leaves_the_handle_attached_when_a_call_never_ends(spied_host, caplog):
    host, fake, touched = spied_host
    release = threading.Event()
    entered = _blocking_poll(fake, release)
    caller = threading.Thread(target=host.poll_events_raw)
    caller.start()
    assert entered.wait(JOIN_S)
    handle = host._h
    assert host.detach(drain_s=0.1) is False
    assert fake.detach_calls == []
    assert ("walk_cancel", handle) in touched   # a blocking walk is cancelled first
    assert "leaving the handle attached" in caplog.text
    release.set()
    caller.join(JOIN_S)
    assert fake.detach_calls == []              # still never freed: a bounded leak


def test_close_joins_the_event_pump_before_detaching(cli_backend, monkeypatch):
    """With no drain allowed in detach, only close()'s pump join lets the handle be freed:
    a poll still running at detach would leave it attached."""
    monkeypatch.setattr(native_mod, "DETACH_DRAIN_S", 0.0)
    fake = FakeNative()
    native_mod.set_backend(fake)
    order = []
    in_poll = threading.Event()

    def poll(h):
        if not in_poll.is_set():
            in_poll.set()
            time.sleep(0.3)
            order.append("poll_end")
        return []
    fake.poll_events = poll
    fake.detach = lambda h: order.append("detach")
    conn = Connection(PID, enable_navigation=False)
    assert in_poll.wait(JOIN_S)
    assert conn.close() is True
    assert not conn._pump.is_alive()
    assert order == ["poll_end", "detach"]


class Stubborn(BotScript):
    """Ignores the stop until released: its thread outlives ScriptRunner.stop()'s wait."""

    def __init__(self):
        self.release = threading.Event()
        self.looping = threading.Event()

    def on_loop(self):
        self.looping.set()
        self.release.wait(JOIN_S)
        return -1


@pytest.fixture
def short_stop(monkeypatch):
    monkeypatch.setattr(runtime_mod, "STOP_JOIN_S", 0.2)


def test_close_leaves_the_handle_attached_while_a_script_thread_runs(cli_backend, short_stop,
                                                                     caplog):
    fake = FakeNative()
    native_mod.set_backend(fake)
    _detach_counter(fake)
    conn = Connection(PID, enable_navigation=False)
    script = Stubborn()
    conn.start_script(script)
    assert script.looping.wait(JOIN_S)
    assert conn.close() is False
    assert fake.detach_calls == []
    assert "still running" in caplog.text
    script.release.set()
    conn._runners["Stubborn"]._thread.join(JOIN_S)
    assert fake.detach_calls == []              # never freed after the fact either


def test_the_manager_sweep_does_not_free_a_dead_host_under_a_live_script(cli_backend,
                                                                         short_stop):
    fake = FakeNative()
    native_mod.set_backend(fake)
    _detach_counter(fake)
    mgr = ConnectionManager(scan_interval_s=3600.0, enable_navigation=False)
    mgr._scan_once()
    conn = mgr.connection(PID)
    script = Stubborn()
    conn.start_script(script)
    assert script.looping.wait(JOIN_S)
    conn._host._alive = False                   # an RPC failed with "client exited"
    mgr._scan_once()
    assert mgr.connection(PID) is None          # swept
    assert fake.detach_calls == []              # but the handle stays attached
    script.release.set()
    conn._runners["Stubborn"]._thread.join(JOIN_S)
    mgr.close()


def test_the_manager_sweep_frees_a_dead_host_once_its_scripts_have_ended(cli_backend):
    fake = FakeNative()
    native_mod.set_backend(fake)
    _detach_counter(fake)
    mgr = ConnectionManager(scan_interval_s=3600.0, enable_navigation=False)
    mgr._scan_once()
    conn = mgr.connection(PID)
    conn._host._alive = False
    mgr._scan_once()
    assert len(fake.detach_calls) == 1
    mgr.close()
