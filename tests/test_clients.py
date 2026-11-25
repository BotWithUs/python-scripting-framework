"""``api/clients.py`` against a fake ``cm_*`` surface: every path, deterministically.

The dispatcher thread is real; the fake's queue hands events out one per poll, as the core
does. Waits are bounded so a regression fails instead of hanging.
"""
import threading
import time

import pytest

from botwithus.api import clients as C
from botwithus.core import native as native_mod
from botwithus.core.native import ProtocolMismatch
from botwithus.host.manager import ConnectionManager

from fake_cm import CM_CONSTANTS, CountingCmNative, FakeCm, FakeCmError, client_row

WAIT_S = 5.0
EV = C.EventKind
ST = C.ClientState


class RecordingAdopter:
    def __init__(self, error: Exception | None = None) -> None:
        self.pids: list[int] = []
        self.error = error
        self.threads: list[str] = []

    def adopt(self, pid):
        self.threads.append(threading.current_thread().name)
        self.pids.append(pid)
        if self.error is not None:
            raise self.error
        return f"conn-{pid}"


@pytest.fixture
def cm():
    return FakeCm()


@pytest.fixture
def adopter():
    return RecordingAdopter()


@pytest.fixture
def launcher(cm, adopter):
    lch = C.ClientLauncher(adopter, backend=cm).start()
    yield lch
    lch.close()


def wait_for(predicate, timeout_s=WAIT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def drain(sub, count, timeout_s=WAIT_S):
    got = []
    deadline = time.monotonic() + timeout_s
    while len(got) < count and time.monotonic() < deadline:
        ev = sub.poll(0.05)
        if ev is not None:
            got.append(ev)
    return got


# --------------------------------------------------------------------------- decoding

def test_every_event_kind_decodes_to_its_typed_event():
    from fake_cm import event
    cases = {
        EV.CLIENT_STARTED: (event(1, client_id="c1", pid=77, state=2, text="acc-1"),
                            C.ClientStarted("c1", 77, ST.SPAWNING, "acc-1")),
        EV.CLIENT_STATE: (event(2, client_id="c1", state=3, text="injecting"),
                          C.ClientStateChanged("c1", ST.INJECTING, None, "injecting")),
        EV.CLIENT_EXITED: (event(3, client_id="c1", state=6, exit_code=1, reason=1),
                           C.ClientExited("c1", 1, C.ExitReason.STOPPED)),
        EV.AGENT_UPDATED: (event(4, text="sha"), C.AgentUpdated("sha")),
        EV.DATA_UPDATE_AVAILABLE: (event(5, text="", hosts_blocking=2),
                                   C.DataUpdateAvailable(None, 2)),
        EV.DATA_UPDATE_APPLIED: (event(6, text="sha2"), C.DataUpdateApplied("sha2")),
        EV.CLOSE_REQUESTED: (event(7, request_id=5, hosts_blocking=1, text="data_update"),
                             C.CloseRequested(5, 1, "data_update")),
        EV.LICENCE_STATE: (event(8, text="reconnecting"),
                           C.LicenceChanged("reconnecting", None, None)),
        EV.SERVICE_SHUTTING_DOWN: (event(9, text="close_clients"), C.ServiceShuttingDown(True)),
        EV.SERVICE_LOST: (event(10, text="service_stopped"), C.ServiceLost("service_stopped")),
        EV.SERVICE_RESTORED: (event(11), C.ServiceRestored()),
        EV.EVENTS_DROPPED: (event(12, text="17"), C.EventsDropped(17)),
        EV.NATIVE_UPDATE_AVAILABLE: (event(13, text="", hosts_blocking=0),
                                     C.NativeUpdateAvailable(None)),
        EV.NATIVE_UPDATE_APPLIED: (event(14, text="sha3"), C.NativeUpdateApplied("sha3")),
    }
    assert set(cases) == set(EV)
    for raw, expected in cases.values():
        assert C.decode_event(raw) == expected


def test_native_update_events_are_never_data_update_events():
    from fake_cm import event
    staged = C.decode_event(event(13, text="ab12", hosts_blocking=0))
    applied = C.decode_event(event(14, text="ab12"))
    assert staged == C.NativeUpdateAvailable("ab12")
    assert applied == C.NativeUpdateApplied("ab12")
    assert not isinstance(staged, C.DataUpdateAvailable)
    assert not isinstance(applied, C.DataUpdateApplied)


def test_failed_state_carries_the_code_not_a_message():
    from fake_cm import event
    ev = C.decode_event(event(2, client_id="c1", state=5, text="spawn_failed"))
    assert ev == C.ClientStateChanged("c1", ST.FAILED, "spawn_failed", None)


def test_licence_event_for_one_client_and_unknown_values_stay_ints():
    from fake_cm import event
    assert C.decode_event(event(8, client_id="c2", state=3, text="live")) == \
        C.LicenceChanged("live", "c2", C.LicenceState.DROPPED)
    started = C.decode_event(event(1, client_id="c1", pid=1, state=99))
    assert started.state == 99 and not isinstance(started.state, ST)
    unknown = C.decode_event(event(42, text="new"))
    assert isinstance(unknown, C.UnknownEvent) and unknown.kind == 42


def test_client_record_from_the_binding_dict():
    row = client_row("c3", 4321, 4)
    rec = C.Client.from_raw(row)
    assert rec.client_id == "c3" and rec.pid == 4321 and rec.state is ST.INJECTED
    assert rec.kind is C.ClientKind.JAGEX and rec.origin is C.Origin.AUTOMATION
    assert rec.is_agent_stale is False and rec.agent_sha == "ab" * 32


# --------------------------------------------------------------------------- surface checks

def test_enum_values_match_the_binding_constant_names():
    for prefix, cls in C._CONSTANT_PREFIXES:
        for member in cls:
            assert CM_CONSTANTS[prefix + member.name] == member.value


def test_start_refuses_another_surface_version(cm, adopter):
    cm.CM_SURFACE_VERSION = 2
    with pytest.raises(ProtocolMismatch):
        C.ClientLauncher(adopter, backend=cm).start()


def test_start_refuses_a_drifted_constant(cm, adopter):
    cm.CM_STOP_KILL = 3
    with pytest.raises(ProtocolMismatch, match="CM_STOP_KILL"):
        C.ClientLauncher(adopter, backend=cm).start()


def test_only_one_launcher_runs_per_process(launcher, cm, adopter):
    with pytest.raises(RuntimeError, match="already running"):
        C.ClientLauncher(adopter, backend=cm).start()
    launcher.close()
    second = C.ClientLauncher(adopter, backend=cm).start()
    second.close()


def test_launch_needs_start(cm, adopter):
    with pytest.raises(RuntimeError, match="start"):
        C.ClientLauncher(adopter, backend=cm).launch("acc-1")
    assert cm.calls == []


def test_close_joins_the_dispatcher_and_ends_subscriptions(cm, adopter):
    lch = C.ClientLauncher(adopter, backend=cm).start()
    sub = lch.events()
    thread = lch._thread
    lch.close()
    assert not thread.is_alive()
    assert list(sub) == []                 # the iterator ends instead of blocking


# --------------------------------------------------------------------------- calls and errors

def test_accounts_and_clients_are_typed(launcher, cm):
    cm.clients_rows = [client_row("c1", 11, 4)]
    assert launcher.accounts() == [C.Account("acc-1", "Account")]
    assert [c.client_id for c in launcher.clients()] == ["c1"]
    assert launcher.resync() == launcher.clients()


@pytest.mark.parametrize("code,cls", [
    ("service_unavailable", C.ServiceUnavailable),
    ("service_stopped", C.ServiceStopped),
    ("rate_limited", C.RateLimited),
    ("bad_argument", C.BadArgument),
    ("session_limit", C.Refused),
    ("account_not_found", C.Refused),
])
def test_cm_errors_become_typed_errors(launcher, cm, code, cls):
    cm.fail = FakeCmError(f"{code} text", code)
    with pytest.raises(cls) as info:
        launcher.accounts()
    err = info.value
    assert isinstance(err, RuntimeError)          # `except RuntimeError` keeps working
    assert err.code == code and err.message == f"{code} text"
    assert err.result == cm.fail.result
    assert err.__cause__ is cm.fail


def test_rate_limited_surfaces_retry_after_ms(launcher, cm):
    cm.fail = FakeCmError("too many launches", "rate_limited", retry_after_ms=17_500)
    with pytest.raises(C.RateLimited) as info:
        launcher.launch("acc-1")
    assert info.value.retry_after_ms == 17_500
    assert launcher._pending == {}


def test_service_down_errors_share_a_parent_and_fail_fast(launcher, cm):
    for code in ("service_unavailable", "service_stopped"):
        cm.fail = FakeCmError("down", code)
        started = time.monotonic()
        with pytest.raises(C.ServiceDown):
            launcher.clients()
        assert time.monotonic() - started < 0.5


def test_missing_cm_error_attributes_use_defaults(launcher, cm):
    cm.fail = FakeCmError("bare", "x")
    del cm.fail.code, cm.fail.result, cm.fail.retry_after_ms
    with pytest.raises(C.Refused) as info:
        launcher.accounts()
    assert (info.value.code, info.value.result, info.value.retry_after_ms) == ("", 0, -1)


def test_non_cm_errors_pass_through_untyped(launcher, cm, monkeypatch):
    monkeypatch.setattr(cm, "cm_accounts", lambda: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError):
        launcher.accounts()


@pytest.mark.parametrize("mode", [True, C.CloseDecision.CLOSING, 7, "kill", None])
def test_stop_rejects_a_bad_mode_and_sends_nothing(launcher, cm, mode):
    with pytest.raises(C.BadArgument):
        launcher.stop("c1", mode)
    assert cm.stops == []


def test_stop_sends_each_mode(launcher, cm):
    launcher.stop("c1", C.StopMode.GRACEFUL)
    launcher.stop("c1", C.StopMode.KILL)
    launcher.stop("c2")
    assert cm.stops == [("c1", 1), ("c1", 2), ("c2", 1)]


@pytest.mark.parametrize("decision", [True, C.StopMode.KILL, 0, "later"])
def test_ack_close_rejects_a_bad_decision_and_sends_nothing(launcher, cm, decision):
    with pytest.raises(C.BadArgument):
        launcher.ack_close(1, decision)
    assert cm.acks == []


def test_launch_rejects_an_empty_account_id(launcher, cm):
    with pytest.raises(C.BadArgument):
        launcher.launch("")
    assert cm.calls == []


# --------------------------------------------------------------------------- launches

def test_launch_hands_the_injected_pid_to_the_manager(launcher, cm, adopter):
    sub = launcher.events()
    handle = launcher.launch("acc-1", 2)
    assert cm.calls[-1] == ("cm_launch", "acc-1", 2)
    cm.push(EV.CLIENT_STARTED, client_id=handle.client_id, pid=555, state=ST.QUEUED,
            text="acc-1")
    cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTING)
    assert not handle.done()
    cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTED)
    assert handle.outcome(WAIT_S) == C.Attached("conn-555")
    assert adopter.pids == [555]
    assert adopter.threads == ["cm-dispatch"]
    kinds = [type(e).__name__ for e in drain(sub, 3)]
    assert kinds == ["ClientStarted", "ClientStateChanged", "ClientStateChanged"]


def test_injected_before_launch_returns_is_still_handed_off(launcher, cm, adopter):
    seen = threading.Event()
    launcher.on_event(lambda e: seen.set() if isinstance(e, C.ClientStateChanged) else None)

    def events_first(client_id):
        cm.push(EV.CLIENT_STARTED, client_id=client_id, pid=601, state=ST.QUEUED)
        cm.push(EV.CLIENT_STATE, client_id=client_id, state=ST.INJECTED)
        assert seen.wait(WAIT_S)           # dispatched before the launch is registered
    cm.launch_hook = events_first
    handle = launcher.launch("acc-1")
    assert handle.outcome(WAIT_S) == C.Attached("conn-601")
    assert adopter.pids == [601]


def test_injected_without_started_looks_the_pid_up(launcher, cm, adopter):
    handle = launcher.launch("acc-1")
    cm.clients_rows = [client_row(handle.client_id, 702, ST.INJECTED)]
    cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTED)
    assert handle.outcome(WAIT_S) == C.Attached("conn-702")
    assert ("cm_clients",) in cm.calls


def test_failed_launch_reports_the_service_code(launcher, cm, adopter):
    handle = launcher.launch("acc-1")
    cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.FAILED, text="inject_failed")
    assert handle.outcome(WAIT_S) == C.Failed("inject_failed", "the launch failed")
    assert adopter.pids == []


def test_exit_before_injection_fails_the_launch(launcher, cm, adopter):
    handle = launcher.launch("acc-1")
    cm.push(EV.CLIENT_STARTED, client_id=handle.client_id, pid=9, state=ST.SPAWNING)
    cm.push(EV.CLIENT_EXITED, client_id=handle.client_id, exit_code=3, reason=4)
    outcome = handle.outcome(WAIT_S)
    assert isinstance(outcome, C.Failed) and outcome.code == C.EXITED_BEFORE_INJECTED


def test_attach_failure_is_reported_not_dropped(cm):
    adopter = RecordingAdopter(OSError("no snapshot mapping"))
    with C.ClientLauncher(adopter, backend=cm) as lch:
        handle = lch.launch("acc-1")
        cm.push(EV.CLIENT_STARTED, client_id=handle.client_id, pid=808, state=ST.QUEUED)
        cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTED)
        assert handle.outcome(WAIT_S) == C.AttachFailed("no snapshot mapping")


def test_clients_launched_elsewhere_are_not_adopted(launcher, cm, adopter):
    cm.push(EV.CLIENT_STARTED, client_id="c99", pid=999, state=ST.QUEUED)
    cm.push(EV.CLIENT_STATE, client_id="c99", state=ST.INJECTED)
    sub_seen = threading.Event()
    launcher.on_event(lambda e: sub_seen.set())
    cm.push(EV.AGENT_UPDATED, text="x")
    assert sub_seen.wait(WAIT_S)
    assert adopter.pids == []


def test_outcome_times_out_while_pending(launcher):
    handle = launcher.launch("acc-1")
    with pytest.raises(TimeoutError):
        handle.outcome(0.05)


def test_close_fails_pending_launches(cm, adopter):
    lch = C.ClientLauncher(adopter, backend=cm).start()
    handle = lch.launch("acc-1")
    lch.close()
    assert handle.outcome(0) == C.Failed(C.LAUNCHER_CLOSED,
                                         "the launcher closed before the launch finished")


# --------------------------------------------------------------------------- resync

def test_restore_resyncs_and_finishes_pending_launches(launcher, cm, adopter):
    injected = launcher.launch("acc-1")
    vanished = launcher.launch("acc-1")
    failed = launcher.launch("acc-1")
    still_starting = launcher.launch("acc-1")
    sub = launcher.events()
    cm.push(EV.SERVICE_LOST, text="service_unavailable")
    cm.clients_rows = [client_row(injected.client_id, 1001, ST.INJECTED),
                       client_row(failed.client_id, 1002, ST.FAILED),
                       client_row(still_starting.client_id, 1003, ST.INJECTING)]
    cm.push(EV.SERVICE_RESTORED)
    assert injected.outcome(WAIT_S) == C.Attached("conn-1001")
    assert vanished.outcome(WAIT_S).code == C.LOST_DURING_OUTAGE
    assert failed.outcome(WAIT_S).code == C.LOST_DURING_OUTAGE
    assert not still_starting.done()
    assert [type(e) for e in drain(sub, 2)] == [C.ServiceLost, C.ServiceRestored]
    assert set(launcher._table) == {c["client_id"] for c in cm.clients_rows}


def test_events_dropped_also_resyncs(launcher, cm, adopter):
    handle = launcher.launch("acc-1")
    cm.clients_rows = [client_row(handle.client_id, 1200, ST.INJECTED)]
    cm.push(EV.EVENTS_DROPPED, text="3")
    assert handle.outcome(WAIT_S) == C.Attached("conn-1200")


def test_a_launch_registered_during_the_resync_is_not_judged_lost(launcher, cm, adopter):
    late = {}

    def launch_while_resyncing():
        cm.clients_hook = None
        late["handle"] = launcher.launch("acc-1")   # queued after the snapshot was taken
    cm.clients_hook = launch_while_resyncing
    cm.push(EV.SERVICE_RESTORED)
    assert wait_for(lambda: "handle" in late)
    time.sleep(0.3)
    assert not late["handle"].done()


def test_a_failed_resync_leaves_pending_launches_waiting(launcher, cm):
    handle = launcher.launch("acc-1")
    cm.fail = FakeCmError("down", "service_unavailable")
    cm.push(EV.SERVICE_RESTORED)
    time.sleep(0.3)
    assert not handle.done()


# --------------------------------------------------------------------------- close requests

def _close_request(cm, request_id=5):
    cm.push(EV.CLOSE_REQUESTED, request_id=request_id, hosts_blocking=1, text="data_update")


def test_no_handler_sends_no_ack_and_keeps_the_request(launcher, cm):
    sub = launcher.events()
    _close_request(cm)
    assert drain(sub, 1) == [C.CloseRequested(5, 1, "data_update")]
    time.sleep(0.2)
    assert cm.acks == []
    assert launcher.pending_close_request == C.CloseRequested(5, 1, "data_update")
    assert launcher._thread.is_alive()


def test_handler_decision_is_acked_from_its_own_thread(launcher, cm):
    threads = []

    def handler(request):
        threads.append(threading.current_thread().name)
        return C.CloseDecision.LATER
    launcher.on_close_requested(handler)
    _close_request(cm, 7)
    assert wait_for(lambda: cm.acks == [(7, 3)])
    assert threads == ["cm-close-7"]
    assert launcher.pending_close_request is None


@pytest.mark.parametrize("result", [None, True, 3, "closing"])
def test_handler_returning_no_decision_sends_no_ack(launcher, cm, result):
    called = threading.Event()

    def handler(request):
        called.set()
        return result
    launcher.on_close_requested(handler)
    _close_request(cm)
    assert called.wait(WAIT_S)
    time.sleep(0.2)
    assert cm.acks == []


def test_handler_that_raises_sends_no_ack(launcher, cm):
    called = threading.Event()

    def handler(request):
        called.set()
        raise ValueError("boom")
    launcher.on_close_requested(handler)
    _close_request(cm)
    assert called.wait(WAIT_S)
    time.sleep(0.2)
    assert cm.acks == []
    assert launcher._thread.is_alive()


def test_explicit_ack_after_returning_none(launcher, cm):
    launcher.on_close_requested(lambda request: None)
    _close_request(cm, 9)
    assert wait_for(lambda: launcher.pending_close_request is not None)
    launcher.ack_close(9, C.CloseDecision.DECLINED)
    assert cm.acks == [(9, 2)]
    assert launcher.pending_close_request is None


def test_a_late_handler_gets_the_pending_request_once(launcher, cm):
    _close_request(cm, 11)
    assert wait_for(lambda: launcher.pending_close_request is not None)
    calls = []
    launcher.on_close_requested(lambda r: calls.append(r.request_id))
    assert wait_for(lambda: calls == [11])
    launcher.on_close_requested(lambda r: calls.append(-r.request_id))
    time.sleep(0.2)
    assert calls == [11]
    assert cm.acks == []


def test_a_late_handler_is_not_called_for_an_acked_request(launcher, cm):
    _close_request(cm, 12)
    assert wait_for(lambda: launcher.pending_close_request is not None)
    launcher.ack_close(12, C.CloseDecision.CLOSING)
    calls = []
    launcher.on_close_requested(lambda r: calls.append(r))
    time.sleep(0.2)
    assert calls == []


def test_a_slow_handler_does_not_block_the_hand_off(launcher, cm, adopter):
    release = threading.Event()
    launcher.on_close_requested(lambda r: release.wait(WAIT_S) and None)
    _close_request(cm)
    handle = launcher.launch("acc-1")
    cm.push(EV.CLIENT_STARTED, client_id=handle.client_id, pid=1300, state=ST.QUEUED)
    cm.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTED)
    assert handle.outcome(WAIT_S) == C.Attached("conn-1300")
    release.set()


# --------------------------------------------------------------------------- subscriptions

def test_every_subscriber_sees_every_event(launcher, cm):
    a, b = launcher.events(), launcher.events()
    cm.push(EV.AGENT_UPDATED, text="s1")
    cm.push(EV.DATA_UPDATE_APPLIED, text="s2")
    assert drain(a, 2) == drain(b, 2) == [C.AgentUpdated("s1"), C.DataUpdateApplied("s2")]


def test_a_full_subscriber_drops_the_oldest_and_says_so():
    sub = C.Subscription(lambda s: None, cap=3)
    for i in range(5):
        sub._put(C.AgentUpdated(str(i)))
    assert sub.poll() == C.EventsDropped(2)
    assert [sub.poll().sha for _ in range(3)] == ["2", "3", "4"]
    assert sub.poll() is None


def test_iterating_blocks_until_an_event_then_ends_on_close(launcher, cm):
    sub = launcher.events()
    got = []
    reader = threading.Thread(target=lambda: got.extend(sub), daemon=True)
    reader.start()
    cm.push(EV.AGENT_UPDATED, text="s")
    assert wait_for(lambda: got == [C.AgentUpdated("s")])
    sub.close()
    reader.join(WAIT_S)
    assert not reader.is_alive()


def test_closed_subscription_receives_nothing_more(launcher, cm):
    sub = launcher.events()
    sub.close()
    cm.push(EV.AGENT_UPDATED, text="s")
    time.sleep(0.2)
    assert sub.poll() is None


def test_a_raising_callback_is_swallowed_and_others_still_run(launcher, cm):
    seen = []
    launcher.on_event(lambda e: (_ for _ in ()).throw(RuntimeError("bad callback")))
    launcher.on_event(seen.append)
    cm.push(EV.AGENT_UPDATED, text="s")
    assert wait_for(lambda: seen == [C.AgentUpdated("s")])
    assert launcher._thread.is_alive()


def test_closing_a_callback_registration_stops_it(launcher, cm):
    seen = []
    with launcher.on_event(seen.append):
        cm.push(EV.AGENT_UPDATED, text="1")
        assert wait_for(lambda: len(seen) == 1)
    cm.push(EV.AGENT_UPDATED, text="2")
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_failing_poll_backs_off_instead_of_spinning(cm, adopter, monkeypatch):
    polls = []

    def failing_poll(timeout_ms=0):
        polls.append(timeout_ms)
        raise FakeCmError("no core", "service_unavailable")
    monkeypatch.setattr(cm, "cm_poll_event", failing_poll)
    with C.ClientLauncher(adopter, backend=cm):
        time.sleep(0.5)
    assert 1 <= len(polls) <= 2


# --------------------------------------------------------------------------- end to end

def test_launch_through_the_real_manager_attaches_once_with_the_scan_running():
    pid = 4400
    be = CountingCmNative(pids=[])
    native_mod.set_backend(be)
    manager = ConnectionManager(scan_interval_s=0.005, enable_navigation=False)
    manager.start()
    try:
        with C.ClientLauncher(manager) as lch:
            handle = lch.launch("acc-1")
            be.push(EV.CLIENT_STARTED, client_id=handle.client_id, pid=pid, state=ST.QUEUED)
            be.pids = [pid]                  # the agent's pipe comes up: the scan can see it
            be.push(EV.CLIENT_STATE, client_id=handle.client_id, state=ST.INJECTED)
            outcome = handle.outcome(WAIT_S)
            assert isinstance(outcome, C.Attached)
            assert outcome.connection is manager.connection(pid)
            time.sleep(0.1)                  # let the scan run over the adopted pid
            assert be.max_open[pid] == 1
            assert manager.connection(pid) is outcome.connection
    finally:
        manager.close()
        native_mod.set_backend(None)
