"""An example management script: launch one client, wait for the lobby, stop it.

Run it in the native host, with the ``botwithus`` package on ``PYTHONPATH``:

    $env:BWU_ACCOUNT = "<account id>"
    bwu_host.exe --py manager.py

``bwu_host`` runs the file without command-line arguments, so the account id comes from the
environment. The script returns normally rather than calling ``sys.exit``: the host shuts its
own service connection down after the file returns. With no account id it lists the
launcher's accounts and the clients the service tracks, then exits. It answers a data-update close request with "later", logs every event, and resyncs
its view of the clients after the service comes back. See docs/CLIENT-MANAGEMENT.md.

It is a self-running script (it defines no BotScript), so the GUI shows it by its file name
and runs it the same way. Its work sits under if __name__ == "__main__": because the GUI
imports every script once to describe it: without the guard, describing it would launch a
client (the host refuses that with "attach during load: guard the self-run").
"""
from __future__ import annotations

import os
import threading
import time

from botwithus.api.clients import (
    Attached, ClientLauncher, CloseDecision, EventsDropped, RateLimited, ServiceRestored,
    StopMode,
)
from botwithus.core.ctl import StopRequested, stop_requested
from botwithus.core.layout import STATE_LOBBY
from botwithus.host.manager import ConnectionManager

LAUNCH_TIMEOUT_S = 300
LOBBY_TIMEOUT_S = 240
LOBBY_POLL_S = 0.5


def answer_later(request):
    print(f"close request {request.request_id}: answering later", flush=True)
    return CloseDecision.LATER


def log_events(launcher, stop):
    with launcher.events() as sub:
        while not stop.is_set() and not stop_requested():
            event = sub.poll(timeout_s=0.5)
            if event is None:
                continue
            print(f"event {event}", flush=True)
            if isinstance(event, (ServiceRestored, EventsDropped)):
                view = launcher.resync()
                print(f"resynced: {[(c.client_id, c.state) for c in view]}", flush=True)


def launch(launcher, account_id):
    while not stop_requested():
        try:
            return launcher.launch(account_id)
        except RateLimited as exc:
            time.sleep(max(exc.retry_after_ms, 100) / 1000.0)
    return None


def wait_for_lobby(connection):
    deadline = time.monotonic() + LOBBY_TIMEOUT_S
    while time.monotonic() < deadline and not stop_requested():
        if connection.snapshot().game_state == STATE_LOBBY:
            return True
        time.sleep(LOBBY_POLL_S)
    return False


def run(launcher, account_id):
    handle = launch(launcher, account_id)
    if handle is None:
        return                         # stopped from the GUI before the launch was queued
    print(f"launched {handle.client_id}", flush=True)
    try:
        outcome = handle.outcome(timeout_s=LAUNCH_TIMEOUT_S)
        print(f"outcome {outcome}", flush=True)
        if isinstance(outcome, Attached):
            print(f"lobby reached: {wait_for_lobby(outcome.connection)}", flush=True)
    except StopRequested:
        # The GUI stopped this script while it waited (only in a GUI worker): close the
        # client it launched and end, inside the GUI's 10 s grace.
        print("stopped from the GUI; closing the client", flush=True)
    launcher.stop(handle.client_id, StopMode.GRACEFUL)


def main(account_id):
    manager = ConnectionManager()
    manager.start()
    stop = threading.Event()
    try:
        with ClientLauncher(manager) as launcher:
            launcher.on_close_requested(answer_later)
            if not account_id:
                print("accounts:", [(a.id, a.name) for a in launcher.accounts()])
                print("clients:", [(c.client_id, c.pid, c.state) for c in launcher.clients()])
                return
            logger = threading.Thread(target=log_events, args=(launcher, stop), daemon=True)
            logger.start()
            run(launcher, account_id)
            time.sleep(2)              # let the exit event arrive before the logger stops
            return
    finally:
        stop.set()
        manager.close()


if __name__ == "__main__":   # only a run executes this; describing the file must not launch
    main(os.environ.get("BWU_ACCOUNT", ""))
