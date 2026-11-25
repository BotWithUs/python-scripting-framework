"""The launch hand-off: ``ConnectionManager.adopt`` against the auto-attach scan.

The invariant: a pid never has two attach handles open at once, whichever path finds it
first. ``CountingNative`` counts a handle as open from the moment ``attach`` is entered, and
can park inside ``attach`` so each test holds the race window open deterministically.
"""
import random
import threading

import pytest

from botwithus.core import native as native_mod
from botwithus.host.manager import ConnectionManager

from fake_cm import CountingNative

PID = 9001
WAIT_S = 5.0


@pytest.fixture
def backend():
    be = CountingNative(pids=[PID])
    native_mod.set_backend(be)
    yield be
    native_mod.set_backend(None)


@pytest.fixture
def manager(backend):
    mgr = ConnectionManager(scan_interval_s=3600, enable_navigation=False)
    yield mgr
    mgr.close()


def _run(target, *args):
    result = {}

    def body():
        try:
            result["value"] = target(*args)
        except Exception as exc:  # surfaced by the assertions
            result["error"] = exc
    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, result


def test_a_scan_parked_in_attach_then_adopt_gets_the_scans_connection(backend, manager):
    gate = backend.park(PID)
    scan, _ = _run(manager._scan_once)
    assert backend.entered[PID].wait(WAIT_S)
    adopt, adopted = _run(manager.adopt, PID)
    adopt.join(0.3)                      # adopt must be waiting on the lock, not attaching
    gate.set()
    scan.join(WAIT_S)
    adopt.join(WAIT_S)
    assert "error" not in adopted
    assert backend.attach_calls[PID] == 1
    assert backend.max_open[PID] == 1
    assert adopted["value"] is manager.connection(PID)


def test_b_adopt_parked_in_attach_then_scan_attaches_nothing(backend, manager):
    gate = backend.park(PID)
    adopt, adopted = _run(manager.adopt, PID)
    assert backend.entered[PID].wait(WAIT_S)
    scan, _ = _run(manager._scan_once)
    scan.join(0.3)
    gate.set()
    adopt.join(WAIT_S)
    scan.join(WAIT_S)
    assert backend.attach_calls[PID] == 1
    assert backend.max_open[PID] == 1
    assert adopted["value"] is manager.connection(PID)


def test_c_adopt_between_discover_and_reconcile_is_not_detached(backend, manager):
    """The scan read the pid list before the client was discoverable; an adopt lands in
    between. Reconciling the stale list must not detach the connection just adopted."""
    backend.pids = []                    # not yet visible to discovery
    discovered = threading.Event()
    proceed = threading.Event()

    def hold_after_discover():
        discovered.set()
        proceed.wait(1.0)
    backend.discover_hook = hold_after_discover
    scan, _ = _run(manager._scan_once)
    assert discovered.wait(WAIT_S)
    backend.discover_hook = None
    adopt, adopted = _run(manager.adopt, PID)
    adopt.join(0.3)
    proceed.set()
    scan.join(WAIT_S)
    adopt.join(WAIT_S)
    assert "error" not in adopted
    conn = manager.connection(PID)
    assert conn is not None and conn.alive
    assert adopted["value"] is conn
    assert backend.open[PID] == 1


def test_d_hammering_both_paths_never_opens_two_handles(backend, manager):
    rng = random.Random(1234)
    stop = threading.Event()

    def worker(seed):
        local = random.Random(seed)
        while not stop.is_set():
            if local.random() < 0.5:
                manager._scan_once()
            else:
                manager.adopt(PID)
            if local.random() < 0.2:
                with manager._lock:
                    manager._detach(PID)  # force fresh races

    threads = [threading.Thread(target=worker, args=(rng.random(),), daemon=True)
               for _ in range(8)]
    for t in threads:
        t.start()
    stop.wait(0.5)
    stop.set()
    for t in threads:
        t.join(WAIT_S)
    assert backend.attach_calls[PID] > 1      # the races did run
    assert backend.max_open[PID] == 1


def test_adopt_returns_the_existing_connection(backend, manager):
    manager._scan_once()
    first = manager.connection(PID)
    assert manager.adopt(PID) is first
    assert backend.attach_calls[PID] == 1


def test_adopt_raises_when_attach_fails_and_scan_only_logs(backend, manager, monkeypatch):
    def refuse(pid):
        raise OSError("no snapshot mapping")
    monkeypatch.setattr(backend, "attach", refuse)
    with pytest.raises(OSError, match="no snapshot mapping"):
        manager.adopt(PID)
    manager._scan_once()                      # logs and swallows, as before A6
    assert manager.connection(PID) is None


def test_adopt_after_close_raises(backend, manager):
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.adopt(PID)
