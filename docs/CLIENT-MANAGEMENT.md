# Client management: writing a management script

A **management script** launches, stops and watches game clients through the BotWithUs
launcher's background service. It is not a `BotScript`: it drives no single client, and it can
run before any client exists. `botwithus.api.clients` is its one entry point.

It sits on the native host's client-management surface (`botwithus._native.cm_*`, version
`CM_SURFACE_VERSION` 1). The native host owns the one service connection of the process. It
registers the host when the process starts, before your script runs, and reconnects by itself
for as long as the process lives. It never starts the service. Your script opens nothing and
closes nothing on the service side.

## The smallest management script

Run it with `bwu_host.exe --py manager.py`, with the `botwithus` package on `PYTHONPATH`
(see the README). `examples/manager_script/manager.py` is a fuller version.

```python
from botwithus.api.clients import Attached, ClientLauncher, StopMode
from botwithus.core.layout import STATE_LOBBY
from botwithus.host.manager import ConnectionManager

manager = ConnectionManager()          # the normal auto-attach scan
manager.start()
try:
    with ClientLauncher(manager) as launcher:
        account = launcher.accounts()[0]
        handle = launcher.launch(account.id)
        outcome = handle.outcome(timeout_s=300)
        if isinstance(outcome, Attached):
            conn = outcome.connection
            print("attached pid", conn.pid, "game state", conn.snapshot().game_state)
        launcher.stop(handle.client_id, StopMode.GRACEFUL)
finally:
    manager.close()
```

**Keep the script running** while it manages clients. When the file returns, `bwu_host`
finalises the interpreter. `bwu_host` passes the script no command-line arguments, so take
settings from the environment. Return normally rather than calling `sys.exit()`: `SystemExit`
ends the process from inside the interpreter, before the host shuts its service connection
down. The launcher stops its own thread on `close()`, at the end of a
`with` block, or at interpreter exit.

## The calls

| Call | Returns | Notes |
|---|---|---|
| `accounts()` | `list[Account]` (`id`, `name`) | No password, PIN or token is ever returned. |
| `launch(account_id, character_index=-1)` | `LaunchHandle` | Returns once the service has **queued** the launch; this can take about 30 s while the service signs in. `character_index` is for Steam accounts. Needs `start()`. |
| `stop(client_id, mode=StopMode.GRACEFUL)` | `None` | `StopMode.GRACEFUL` or `StopMode.KILL`. Works on any client, whoever launched it. A `ClientExited` event follows. |
| `clients()` | `list[Client]` | Every client the service tracks. This is the source of truth. |
| `resync()` | `list[Client]` | The same as `clients()`; the name says why you are calling it. |
| `events()` | `Subscription` | See *Events*. |
| `on_event(callback)` | a registration; `close()` it to stop | See *Events*. |
| `on_close_requested(handler)` | `None` | See *Close requests*. |
| `ack_close(request_id, decision)` | `None` | See *Close requests*. |

Any call can block for up to 40 s waiting for the service. The native host releases the GIL
meanwhile, so your other threads keep running.

**Limits.** `clients()` returns at most 128 rows and `accounts()` at most 256. The native
binding truncates beyond that **without saying so**. The service caps live clients at 63, so
this does not bite in practice.

## Launches and the hand-off

`launch()` returns a `LaunchHandle`. Its `outcome(timeout_s)` blocks until the launch ends,
and raises `TimeoutError` if the timeout passes first. The outcome is one of:

- `Attached(connection)`. The service reported the client `INJECTED`, and the launcher handed
  its pid to your `ConnectionManager`. `connection` is the manager's own `Connection`, the same
  object `manager.connection(pid)` returns. Read the snapshot from it to wait for the lobby.
- `Failed(code, message)`. The launch failed before injection. `code` is the service's failure
  code. Three codes come from the framework itself:
  - `exited`: the process exited before it was injected;
  - `lost_during_outage`: the client failed, exited or vanished while events were missed;
  - `launcher_closed`: the launcher was closed first. The client itself keeps running.
- `AttachFailed(message)`. The client was injected, but attaching to it failed. For example,
  the agent's snapshot mapping is missing, in which case the attach fails at once.

**A pid is never attached twice.** The manager's auto-attach scan may find the new client at
the same moment the launcher hands it over. Both paths claim the pid under the manager's lock,
so whichever comes second receives the first one's `Connection`. Do not call `Connection(pid)`
yourself for a launched client: use the outcome, or `manager.connection(pid)`.

**After an auto-restart**, the service starts the client again with a new pid. The handle has
already completed; the scan attaches the new process. `ClientStarted` tells you the new pid.

## Events

One dispatcher thread per launcher drains the service's events. For each event it first
updates the launcher's own client table and finishes any launch it completes, then copies the
event to every subscriber. You read events in either of two ways.

**A subscription.** `launcher.events()` returns a `Subscription` with its own queue:

```python
with launcher.events() as sub:
    for event in sub:                  # blocks; ends when the subscription is closed
        ...

event = sub.poll(timeout_s=0.5)        # or poll it from a loop that does other work
```

- A subscription sees only events dispatched after it was created.
- It holds at most 1,024 events. If you fall behind, the oldest is dropped, and your next read
  returns one `EventsDropped(count)` first.

**A callback.** `launcher.on_event(callback)` calls `callback(event)` **on the dispatcher
thread**. Keep it short: while it runs, no launch is handed over and no other subscriber gets
the event. An exception it raises is logged and ignored.

The events are frozen dataclasses, so `match` works:

| Event | Fields | When |
|---|---|---|
| `ClientStarted` | `client_id`, `pid`, `state`, `account_id` | A launch reached its first record, or an auto-restart replaced the process. |
| `ClientStateChanged` | `client_id`, `state`, `code`, `message` | A launch moved on. `code` is set only for `FAILED`, `message` only otherwise. `INJECTED` is the last state the service knows. |
| `ClientExited` | `client_id`, `exit_code`, `reason` | The process exited. `reason` is an `ExitReason`. |
| `AgentUpdated` | `sha` | The launcher has a new agent; clients injected before it are now stale. |
| `DataUpdateAvailable` | `staged_sha`, `hosts_blocking` | A data update changed state. |
| `DataUpdateApplied` | `sha` | A data update was applied. |
| `NativeUpdateAvailable` | `staged_sha` | The native host package changed state. It is not a data update, and no host blocks it. Needs a launcher with the `native` topic; an older one never sends it. |
| `NativeUpdateApplied` | `sha` | A native host package version is now current. A running host keeps running from its own version. |
| `CloseRequested` | `request_id`, `hosts_blocking`, `reason` | See *Close requests*. |
| `LicenceChanged` | `link`, `client_id`, `state` | The licence link changed (`client_id` is None), or one client's licence did. |
| `ServiceShuttingDown` | `close_clients` | The service is shutting down. |
| `ServiceLost` | `code` | The host lost the service: `service_unavailable` or `service_stopped`. Sent once per loss. |
| `ServiceRestored` | none | The host has the service again. Never sent for the first connection. |
| `EventsDropped` | `count` | Events were missed. |
| `UnknownEvent` | `kind`, `raw` | A newer host sent a kind this framework does not know. |

An enum value the framework does not know is kept as a plain `int`, never an error.

**Fields the Java host has and Python does not.** The native surface does not carry them, so
they are left out rather than guessed:

- `DataUpdateAvailable`: `phase` and `applyWhenHostsClose`;
- `LicenceChanged`: `failures` (read `licence_failures` from `clients()`);
- `ClientStateChanged`: `stage`.

## After the service comes back: resync

Events that happened while the service was away are **not replayed**. The same is true after
`EventsDropped`. When you see `ServiceRestored` or `EventsDropped`, call `clients()` and
rebuild whatever view your script keeps:

```python
for event in sub:
    match event:
        case ServiceRestored() | EventsDropped():
            view = {c.client_id: c for c in launcher.resync()}
        case ClientStateChanged(client_id=cid, state=state):
            ...
```

The launcher already does this for its own pending launches: an injected client is handed
over, and a failed, exited or vanished one completes as `Failed("lost_during_outage", ...)`.

## Close requests

When a data update is waiting, the service may ask this host to close. The native host prints
one line to stderr and queues a `CloseRequested`. It never answers by itself, never prompts and
never exits.

```python
from botwithus.api.clients import CloseDecision

def on_close(request):
    if nothing_important_running():
        shutdown.set()                 # your own way to end the script
        return CloseDecision.CLOSING
    return CloseDecision.LATER

launcher.on_close_requested(on_close)
```

- **The handler runs on its own short thread**, never on the dispatcher, so it may take its
  time.
- **Return a `CloseDecision`** (`CLOSING`, `DECLINED` or `LATER`) and the launcher acks with it.
- **Return `None`** to send nothing now. Call `launcher.ack_close(request.request_id,
  decision)` later, from anywhere.
- If the handler **raises**, or returns anything else (including `True`), it is logged and
  **nothing is sent**.
- **With no handler registered, nothing is sent and nothing exits.** "No answer" is a normal
  state. The launcher shows the host as waiting, and the update applies by itself once the host
  exits.
- **The framework never exits the process,** not even after `CLOSING`. If you answer `CLOSING`,
  ending the script is your job.
- A request that arrived before you registered a handler is kept as
  `launcher.pending_close_request`. It is delivered once to the next handler you register,
  unless it has been acked in the meantime.
- An ack changes only what the launcher displays. It never terminates anything.

## Errors

Every failed call raises a `ClientsError`, which is a `RuntimeError`, so existing
`except RuntimeError` code keeps working. Each carries `code` (the thing to branch on),
`message`, `result` and `retry_after_ms`. The original `botwithus._native.CmError` is
`__cause__`.

| Class | `code` | Meaning |
|---|---|---|
| `ServiceUnavailable` (a `ServiceDown`) | `service_unavailable` | The service is not running, is restarting, crashed, or did not answer within 40 s. Raised quickly; the host keeps reconnecting. |
| `ServiceStopped` (a `ServiceDown`) | `service_stopped` | The user stopped the service from the tray. |
| `RateLimited` | `rate_limited` | Too many launches. Wait `retry_after_ms` milliseconds. |
| `BadArgument` | `bad_argument` | A bad argument. Nothing was sent. |
| `Refused` | the service's code | `account_not_found`, `client_not_found`, `not_signed_in`, `session_limit`, `concurrency_cap`, `licence_unavailable`, ... |

```python
try:
    handle = launcher.launch(account.id)
except RateLimited as exc:
    time.sleep(exc.retry_after_ms / 1000)
except ServiceDown:
    ...                                # wait for ServiceRestored
```

## Rules

- **Only one `ClientLauncher` may run per process.** The native event queue is process-wide
  and has a single reader. A second launcher would silently split the events, so `start()`
  refuses it. Share the one you have.
- `launch()` needs `start()` (or a `with` block). Without the dispatcher, nothing would
  complete the launch.
- Outside `bwu_host`, `botwithus._native` does not exist. Calls raise `NativeUnavailable`,
  unless a test injected a fake with `core.native.set_backend()`.
