# CONTEXT — python-scripting-framework

Orientation for anyone (human or agent) working in this repo. The README is for script
authors; this is for maintainers of the framework itself.

## What this is

The **Python frontend of the native hybrid host** (`native-scripting-host`) — one of two
open scripting APIs on it, the sibling being `lua-scripting-framework`. It no longer talks
to the agent directly; it runs as the CPython that `bwu_host.exe` embeds and reaches the
agent through the host's builtin `botwithus._native` module. Its peers:

| Consumer | Repo | Language | Role |
|---|---|---|---|
| Agent (producer) | `NXTLibrary` | C++ | Injected DLL; owns the wire contract |
| Native hybrid host | `native-scripting-host` | C/C++ | Embeds CPython + Lua; owns the transport + SDN seam |
| Java host | `JBotWithUsV2` | Java | Reference host + script framework |
| C# host | `CSharpScriptingFramework` | C# | Structural blueprint we mirror |
| **This** | `python-scripting-framework` | Python | Python author API on the native host |

The wire format is defined by `NXTLibrary/docs/PUBLIC_PROTOCOL.md`, but this repo no longer
implements it — the native host does. `layout.py` still decodes the raw snapshot/event
**bytes** the host hands back, so it stays a mirror of `SharedLayout.h`/`Events.h`.

## Layering

- `botwithus.core.native` — **the one seam onto `botwithus._native`.** `NativeHost` wraps the
  host capsule: `snapshot_bytes()`, `rpc()`, `poll_events_raw()`, `discover_pids()`,
  `read_varps()` / `read_varbits()` (`bwu_read_varps` / `bwu_read_varbits`). Every
  other core module goes through it; nothing here opens a pipe or maps shared memory anymore.
- `botwithus.core` (rest) — the MessagePack codec, the snapshot/event **decoders** (pure
  Python over `layout.py`, fed raw bytes by the host), the per-client `Connection`, and the
  script loader. Pathing routes through `NativeHost.path` (`bwu_path`); there is no ctypes
  here anymore.
- `botwithus.api` — the script-author contract: `BotScript`, `ScriptContext`, snapshot
  dataclasses, fluent entity/inventory/component queries, actions, events, navigation.
- `botwithus.host` — `ConnectionManager` (auto-attach scan loop) and a maintainer's start/stop
  REPL in `__main__`. There is no console script: `_native` exists only inside `bwu_host`, so the
  REPL exits with a one-line "run scripts through bwu_host" message under a plain Python.
  `ConnectionManager.adopt(pid)` is the hand-off from `api.clients`: the scan and the adopt
  claim a pid under one lock, so a launched client is never attached twice.
- `botwithus.api.clients` — the management-script entry point (`ClientLauncher`) over the
  native `cm_*` surface: launch, stop, list, events, and the data-update close hook. One per
  process, because the native event queue it drains is process-wide. See
  `docs/CLIENT-MANAGEMENT.md`.

## Where the traps went

The transport traps the Java/C# hosts paid for — one `write()` per frame, one lock over all
pipe I/O, hang-up invisible to `PeekNamedPipe`, id correlation, push-skipping, and the SHM
`magic`/`version` bind — **now live in the native host** (`AgentPipe` / `SnapshotRegion` in
`host_surface.cpp`), verified there. What this repo must still respect:

1. **`layout.py` is the wire-contract mirror.** It decodes the raw bytes the host returns, so
   any change to `SharedLayout.h`/`Events.h` lands here in lockstep. (The host self-pins its
   own vendored copy with a `sizeof(Snapshot)` static_assert; this side pins offsets in
   `test_layout.py`.)
2. **The three clocks are not interchangeable** — pace on `serverTick` only. See README.
3. **Bind everything to the pid.** A `NativeHost` / `Connection` never migrates pids.
4. **Varp state is composed in the native host, not here.** `bwu_read_varps` combines the
   agent's per-id state with the cache's varp default; `core/variables.py` only maps the
   records to `VarpRead` / `VarbitRead`, and maps state and kind numbers through the native
   module's own `VARP_*` / `VAR_KIND_*` constants **by name**, so a renumbering on the host
   cannot silently misread. A native host without them is refused loudly (`NativeUnavailable`).
5. **Navigation routes through the native host** — `SceneWalker` calls `NativeHost.path`
   (`_native.path` → `bwu_path` → the host's linked worldwalker). No ctypes, no
   `worldwalker.dll` load on the Python side. v1 is query-first (plan, then queue a WALK
   toward the furthest reachable step); the blocking native executor is a follow-up.
6. **Plugin UI is owned per script, but the pipe is per `Connection`.** (P2 adds hook
   subscriptions: they are the script's too, tracked per handle in `PluginUi._hooks`, and go with
   the component on both sides, so a destroy, a `destroyed` event or the release sends no
   `ui.unsubscribe`. Typed hook events are `UiEvent` subclasses; `ScriptEventBus` dispatches a
   script-scoped event by its MRO, so `subscribe(UiEvent, h)` keeps getting every one.) The agent owns `ui.*`
   components per pipe connection, and the native host releases a whole `BwuHost` with one
   `ui.destroy_all` at detach. One `Connection` (one `BwuHost`) can run several scripts
   (`start_script`). So each `ScriptContext` gets its own `api.ui.PluginUi`, which tracks the
   handles that script created. After `on_stop`, the runner and the worker destroy exactly
   those handles with one `ui.destroy` each, newest first. The framework **never** sends
   `ui_destroy_all`.

   **`ui_poll_events` never runs on the ring pump.** It waits on the host's I/O lock, which any
   in-flight pipe call holds for up to 5 s, so on the pump one script's slow ui call would
   stall every script's ServerTick, chat and varp events. `core/ui_events.UiEventThread`
   polls it on its own 20 ms thread instead. The thread starts on the first ui call any script
   makes and ends once no live UI script remains, then starts again on the next first use.

   `api.ui.UiRouter`, one per `Connection`, routes each event by handle to the script(s)
   holding it, onto their `ScriptEventBus`:
   - A component's event goes to the script that made it.
   - A Jagex handle's event goes to every script that looked it up. The agent dedups
     `ui.get_component` per connection, so scripts share the handle.
   - `overflow` goes to every live UI script.
   - An event for a handle no script holds yet, because it raced the `create` that returns the
     handle, is kept briefly and delivered once the script claims the handle. One lock covers
     the claim and the routing.

   `UiEvent` is script-scoped; game events still go on the shared bus. Binding errors pass through
   unchanged (`UiRefused`, `ValueError`, `TimeoutError`). Only a transport-dead text becomes
   `ConnectionError` / `ClientExited`.

## Native host version history: "No tile to route from"

The README describes only what the shipped host does. These are the host commits that
changed it, for anyone reading an old run log or running an older `bwu_host`. Both are in
native-scripting-host 3547bc1, the host the 1.0.0 release is built from.

**A player gone by then** (the snapshot has no local player when the host acts) is the host's
refusal from native-scripting-host 70f6139 on: `walk` returns `False`, and `walk_ex`, `start`,
`walk_to` and `plan` raise `PathError` containing `not in game`. Before 70f6139, `walk_ex` and
`start` do not refuse: the walk starts.

**A player still there with no tile** depends on the host:

| | From native-scripting-host b814645 | Older hosts, from 70f6139 |
|---|---|---|
| `walk` | `False`, no walk started | `False`, if the tile is still missing when the walk starts |
| `walk_ex`, `start` | `PathError` containing `not in game: no tile yet`, no walk started | a failed `WalkResult` from `walk_ex` or `wait`, on the same condition |
| `walk_to`, `plan` | `PathError` containing `not in game: no tile yet` | `PathError` from the planner, containing `no route` |

From b814645 on, a walk that has started reads a tile lost later as the last tile it saw,
instead of `-1`.

## The action queue is all or nothing

From NXTLibrary #67 (0a0e713) the agent queues a whole `queue_actions` batch or refuses it
whole (`queue_actions: queue_full` / `batch_too_large` / `action_id_required` /
`bad_param <field>`), queuing none of it. This framework sends the call through `rpc_raw`,
so a refusal arrives as the agent's text and `GameApi` raises `ActionsRefused` (nothing
queued); the host's `rpc timeout` (or `rpc_timeout`) becomes `ActionOutcomeUnknown` and a dead pipe
`ConnectionError` (both: may or may not be queued). A v25 agent from before #67 can still
reply with a queued prefix, so `queue_actions` returns that count rather than refusing it.
The agent side is NXTLibrary #67 (0a0e713), `src/rpc/Handlers.cpp` `Handle_QueueActions`.
`ActionsRefused` is the only error that means nothing was queued; `rpc protocol error` is
also `ActionOutcomeUnknown`, and any other error leaves the outcome unknown too.

## Provenance

New repo, salvaged the API shape and script runtime from the author's earlier
`BWUPythonScriptingFramework` spike. The transport was first rebuilt as a ctypes SHM+pipe
client against protocol v19, then **reconciled onto the native hybrid host** — the ctypes
`pipe.py`/`shm.py` were deleted and the wire layer now rides `botwithus._native`.
