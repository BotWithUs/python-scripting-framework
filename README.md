# python-scripting-framework

Write **BotWithUs scripts in Python**. This is the Python API of the BotWithUs native host
(`bwu_host.exe`, from `native-scripting-host`); the sibling `lua-scripting-framework` is the
Lua one. Your script runs in the CPython 3.12 that `bwu_host` embeds. The framework hands it
live game state and takes its requests, so you write game logic, not wire calls.

## Quickstart

### Where scripts live

Keep your Python scripts in **`~/.botwithus/scripts/python`**
(`%USERPROFILE%\.botwithus\scripts\python`), beside `~/.botwithus/scripts/lua` for Lua.
That is the folder the `bwu_host` GUI lists, and the one the framework's script loader reads
(`BWU_PY_SCRIPTS_DIR` overrides it). `bwu_host --py` runs whatever path you give it.

### The shape of a script

A script is **one** `BotScript` subclass with a `script_manifest`, in its own file. Everything
a script normally needs imports from `botwithus.api`:

```python
from botwithus.api import BotScript, ScriptContext, script_manifest


@script_manifest(name="Hello", version="1.0", author="you")
class Hello(BotScript):
    def on_start(self, ctx: ScriptContext) -> None:
        self.ctx = ctx
        self.loops = 0

    def on_loop(self) -> int:
        me = self.ctx.self_player
        if me.has_tile:                     # a player in this frame, with a tile
            self.ctx.log.info("standing on %s", me.tile)
        self.loops += 1
        return -1 if self.loops >= 10 else 600   # ms until the next call; negative stops


if __name__ == "__main__":                  # only `bwu_host --py hello.py` runs this
    import time

    from botwithus.core.connection import Connection
    from botwithus.core.native import default_client_pid

    pid = default_client_pid()
    if pid is None:
        raise SystemExit("no game client found")
    conn = Connection(pid)
    conn.start_script(Hello())
    while conn.running_scripts():
        time.sleep(0.5)
    conn.close()
```

Lifecycle is `on_start(ctx)` → `on_loop()` (repeated) → `on_stop()`. `on_loop` returns
the delay in **milliseconds** before the next call, or a negative value to stop. Each
script runs on its own thread and **paces itself against `serverTick`**: see "The three
clocks" below. `ctx.stop_self()` and `ctx.is_stop_requested()` (or the property
`ctx.stopping`) are the cooperative stop.

### Running it

**In the GUI.** Start `bwu_host` with no arguments. It lists every `.py` file in
`~/.botwithus/scripts/python` by its manifest, and runs the one you pick against the client
you pick, each run in its own worker process. To list a file the GUI imports it once, as the
module `bwu_script` (not `__main__`), and looks for exactly one `BotScript` defined in it; so
keep the file free of work at import time. A file with two `BotScript` classes fails to load.
A file with none is a *self-running* script: the GUI lists it by its file name and runs it
as `__main__`, the way `--py` does; it must keep its work under
`if __name__ == "__main__":`, or describing it runs it.

**From a console.**

```powershell
bwu_host --py $HOME\.botwithus\scripts\python\hello.py
```

`bwu_host --py` runs the file directly as `__main__`, which is why the script above ends with
the `if __name__ == "__main__":` block: it attaches to the first game client and runs the
script until it stops. With no client running it exits with `no game client found`. The GUI
never runs that block for a plugin script; it drives the class itself.
`sys.argv` is `[script, extra args...]`, so `bwu_host --py hello.py a b` gives
`['hello.py', 'a', 'b']`.

`default_client_pid()` picks the client: an explicit pid if you pass one; when the GUI runs
a self-running script, the client the user chose (`BWU_CLIENT_PID`); otherwise the first
client found.

### Settings

A script declares its settings with `get_config_fields()`; the GUI draws them in the
script's panel and saves them per script. The values in force arrive in
`on_config_update(config)`, **once before `on_start`** and again after each change the user
commits, on the script's thread between `on_loop` calls. `ctx.config` holds the same values
as a read-only mapping.

```python
from botwithus.api import BotScript, choice_field, int_field, script_manifest


@script_manifest(name="Cutter", category="Woodcutting")
class Cutter(BotScript):
    def get_config_fields(self):
        return [int_field("logs", "Logs to cut", 28, min=1, max=1000),
                choice_field("tree", "Tree", ["Oak", "Willow", "Yew"], default="Willow")]

    def on_config_update(self, config):
        self.logs, self.tree = config["logs"], config["tree"]

    def on_loop(self) -> int:
        return 600
```

The kinds are `int_field`, `string_field`, `bool_field`, `choice_field` and `item_id_field`
(-1 is "no item"), like the Java host's `ConfigField`. The host is stricter than Java in four
places, and the framework refuses them when the script is listed or started: a `choice`
default must be one of its options, a `choice` needs options, an `item_id` default cannot be
below -1, and a `string` default longer than 256 bytes needs `max_len=`. Under `--py` the
settings are their defaults. `author` defaults to `""` (shown as "Unknown author");
`category` groups the script in the GUI.

### Stopping

When the GUI stops a script, `ctx.sleep(ms)` and `ctx.sleep_ticks(n)` return `False` within
250 ms, no further `on_loop` runs, and `on_stop` is called. The whole stop must finish within
10 seconds or the worker is terminated. The framework never interrupts your code: a script
blocked in `time.sleep` or a long call of its own sees the stop only when that returns, so
wait through `ctx.sleep` / `ctx.sleep_ticks`, and check `ctx.stopping` in long loops. A
self-running script checks `botwithus.api.stop_requested()` instead; there,
`LaunchHandle.outcome()` raises `StopRequested` on a stop. If the game client exits, a GUI run
ends the same way: no further `on_loop`, then `on_stop`. See `CHANGELOG.md` for what changed
for existing scripts.

The framework does not open a pipe, map shared memory or load the pathfinder itself. The
native host owns the transport, the snapshot and its version check, the event ring, the
worldwalker pathfinder and its executor, and the SDN decrypt seam. This framework reaches
all of it through one builtin module, `botwithus._native`, which exists **only inside
`bwu_host`**. That is why there is no `botwithus` command and no `python -m` entry point:
under a plain Python they could do nothing but fail.

## Breaks

`ctx.game.schedule_break(seconds)` holds the action queue for that many **seconds**
(fractions allowed): `schedule_break(300)` is a five-minute break. The agent counts breaks
in milliseconds, so the framework converts before sending, and refuses anything under 1 ms
or over about 24.8 days (`MAX_BREAK_SECONDS`, the agent's 2³¹−1 ms limit) with a
`ValueError` instead of sending it. `interrupt_break()` ends a break early. The
`BreakStarted` event reports the length in whole seconds.

## Logging and run logs

Use `ctx.log` rather than `print`:

```python
def on_loop(self) -> int:
    self.ctx.log.debug("checking bank at %s", tile)
    self.ctx.log.warn("no food left")
```

Under `bwu_host`, every run of a script writes one file under
`~/.botwithus/logs/scripts/<script-slug>/`, redacted before it reaches disk (account and
character names, emails, tokens, IPs, your user folder). It holds DEBUG and up whatever the
console shows: `ctx.log`, the standard `logging` module, and `print` / `sys.stderr` from the
script's thread. When a script stops on an error, the file ends with a crash block: the phase
(`on_start`, `on_loop` with its iteration, `on_stop`), the traceback, the innermost frame in
your code, and the last 200 host calls the script made. The native host owns the file and the
redaction; this framework only tells it the phase. Threads a script starts itself are not
attributed to its run.

## Managing clients

A **management script** launches, stops and watches game clients through the launcher's
background service, instead of driving one client. Its entry point is
`botwithus.api.clients.ClientLauncher`, which hands each client it launches to the
`ConnectionManager` once the client is injected, so the client is never attached twice. Read
[`docs/CLIENT-MANAGEMENT.md`](docs/CLIENT-MANAGEMENT.md) for the calls, events, the
data-update close hook, errors and the resync rule. `examples/manager_script/manager.py` is a
runnable example.

## The three clocks — the easiest thing to get wrong

The snapshot exposes three counters that live in three different number spaces:

- **`serverTick`** — the 0.6 s server tick. **This is the only clock scripts should pace
  against.** `-1` until a world is loaded.
- **`gameCycle`** — the client's ~20 ms frame counter, and the unit projectile
  `startCycle`/`endCycle` are stamped in. `0` in the lobby is normal.
- **`publishSeq`** — the agent's private publish counter, same cadence as `gameCycle` but
  a **different number space**. Never compare or subtract it against `gameCycle`.

Pacing off `publishSeq` instead of `serverTick` runs a script ~30× too fast — a bug that
has already shipped once elsewhere. Use `sleep_ticks(n)`, which waits for `serverTick` to
advance by `n`.

## Pathfinding

The agent does not path. Walking is the consumer's job, and the native host does it: it
links `worldwalker.dll` (the same pathfinder the first-party hosts use, shipped by the
launcher into `~/.botwithus/native/`) and exposes both a query and a blocking executor. No
pathfinder runs on the Python side; `ctx.navigation` is a `SceneWalker` with two styles:

- `walk_to(goal)` — query-first, called once per loop: queues a WALK toward the furthest
  reachable step and returns whether you've arrived. Only walks *up to* transitions. When there
  is no tile to route from (see below) it returns `False`, with nothing planned or queued. So a
  `while not walk_to(goal)` loop waits rather than crashing: keep checking
  `ctx.is_stop_requested()` in it.
- `walk(goal)` — a **blocking** full walk through the native executor: it walks the route and
  *executes* its transitions (doors/stairs/teleports/dialogue/item chains), returning on
  arrival. `cancel()` (from another thread) stops it. `False`, walking nothing, when there is no
  tile to route from.
- `walk_ex(goal)` — the same blocking walk, returning a `WalkResult` (`botwithus.core.walk`):
  status, the step and transition a failed walk failed on (`failed_on_transition`: the loc,
  NPC or chain the walker tried), re-plans used, elapsed time and every walker event.
- `start(goal)` / `events(since)` / `wait(timeout_ms)` / `status()` / `cancel()` — the
  non-blocking form, so a script can watch progress and step in on `WALK_EV_STUCK`:

```python
from botwithus.core import walk as W

nav = ctx.navigation
# has_progress is False on a bwu_host that predates it; for has_tile see "No tile to route from"
if nav.has_progress and ctx.self_player.has_tile:
    nav.start(goal, radius=2)
    seen = 0
    while (r := nav.wait(600)).is_running:
        evs = nav.events(seen); seen += len(evs)
        if any(e.kind == W.WALK_EV_STUCK for e in evs):
            nav.cancel()
    if (t := r.failed_on_transition) is not None:
        print("failed on", t.kind, t.object_id, t.tile, t.interact_attempts)
```

### No tile to route from

A route starts from the player's tile, and `me.valid` does not promise one. By the agent's source
a valid player has no tile in two shapes:

- all three fields `-1`, while it has no scene entity;
- x and y `-1` over a real plane, while that entity has no scene graph node. The agent calls this
  normal and transient while the engine unlinks an entity from the scene.

`me.has_tile` is the test: a player in the frame with all three fields set. It reads the visual
tile only, so a server tile of `-1`, which is every frame on the beta channel, stops nothing.
Without it:

| Call | Result | The host |
|---|---|---|
| `walk_to`, `walk` | `False` | not called |
| `plan`, `walk_ex`, `start` | raises `PathError` containing `not in game` | not called |

An uncaught `PathError` ends a script, so gate those three on `me.has_tile`:

```python
me = ctx.self_player
if not me.has_tile:
    return 1000          # wait: nothing has a position yet
result = ctx.navigation.walk_ex(goal)
```

This is a check of one frame, not a lock, so the host can see something else when it acts:

- `walk`, `walk_ex`, `start` read their own frames on the host.
- `walk_to`, `plan` plan from the frame the check just filled. That frame is shared per host, so a
  change reaches them only if another thread on the connection, such as a second script, refreshes
  the snapshot in between.

**A player gone by then** is the host's refusal: `walk` returns `False`, and `walk_ex`, `start`,
`walk_to` and `plan` raise `PathError` containing `not in game`.

**A player still there with no tile** is refused the same way, before anything moves:

| Call | Result |
|---|---|
| `walk` | `False`, no walk started |
| `walk_ex`, `start` | `PathError` containing `not in game: no tile yet`, no walk started |
| `walk_to`, `plan` | `PathError` containing `not in game: no tile yet` |

The Python check raises `not in game: no tile yet` for every frame it stops, including no player,
the lobby and no client, so the phrase does not mean the player is still there. Only `has_tile` and
`valid` tell those apart.

A walk that has started reads a tile lost later as the last tile it saw, instead of `-1`. What
that does next comes from reading the walker's source, not from a test or a run. On a
walk step a short loss is tolerated: it looks like a player who has stopped moving, which costs a
re-click and then a re-plan. Around a teleport or a crossing, a stale read can look like a landing
that did not happen, so the walker may click again, route around the transition, or fail. A player
who leaves the frame in the middle of a walk is not covered by this.

`NativeHost.path` and its walks have no Python check, only the host's. How often a player has no
tile on a live client, and for how long, has not been measured.

### Transitions

Every transition a walk attempted is logged, not only the last one. Each `WalkTransition` is
what the walker was *seen to do* (the loc it interacted with, the NPC it clicked, the chain
steps it ran), and its `baked` field is a `TransitionInfo`: what the walker's *data says* the
transition is (origin, kind, origin and destination tiles, requirements, the chain verbatim,
the planner's cost). `baked` is None for index -1, for an unknown index, and when the walker
cannot describe transitions.

- `walk_ex(goal).transitions` — the whole log, in order; a transition retried after a re-plan
  is a new entry. `transitions_cut` is True when the walk attempted more than the host keeps
  (`WALK_MAX_TRANSITIONS`).
- `transitions(since)` — the log of the current or last walk at any time, with a cursor like
  `events(since)`; use it to follow a `start(goal)` walk.
- `describe(index)` — the `TransitionInfo` for any transition index (a `WalkEvent` or
  `WalkTransition` index), or None for no such transition. Raises `PathError` when the walker
  cannot describe at all.

These are feature-detected: on a bwu_host that predates the log, `has_transitions` is False,
`walk_ex` still works with `transitions` empty and `baked` None, and `transitions()` /
`describe()` raise `NativeUnavailable`.

```python
if ctx.self_player.has_tile:              # walk_ex raises without one: "No tile to route from"
    r = nav.walk_ex(goal)
    for t in r.transitions:
        b = t.baked
        print(t.transition_index, t.kind, t.interact_issued,
              None if b is None else (b.origin, b.dest_tile, b.requirements))
```

### Walk options: route around a transition that failed

`plan`, `walk`, `walk_ex`, `start` and `walk_to` (and `NativeHost.path` / `walk` / `walk_ex` /
`walk_start`) take three keyword-only options:

- `exclude=[...]`: transition indices the planner must not use, on the first plan and on
  every re-plan. Use `WalkResult.fail_transition` or `WalkTransition.transition_index`.
- `exclude_loc_siblings=True`: widen each excluded index to every row of the same loc.
- `disabled_moves=W.moves_mask(W.MOVE_CHARTERS, ...)`: movement categories switched off. It
  can be ORed with `W.RESTRICT_FREE_TO_PLAY`. The host's `BWU_WW_RESTRICT_F2P` bit is always
  added.

```python
if ctx.self_player.has_tile:              # walk_ex raises without one: "No tile to route from"
    r = nav.walk_ex(goal)
    if r.status == W.WALK_FAILED and r.fail_transition >= 0:
        r = nav.walk_ex(goal, exclude=[r.fail_transition], exclude_loc_siblings=True)
```

Options are never silently ignored:

- With every option at its default, the call is the one made before options existed.
- Any other value on a bwu_host that predates options raises `NativeUnavailable`
  (`nav.has_options` is False).
- An exclusion on a worldwalker.dll older than WorldWalker #26 raises
  `WalkOptionsUnsupported`, a `PathError` subclass, before anything moves.
  `nav.options_supported()` tells you in advance.
- An index the walker no longer knows (runtime teleport indices change on every reload) comes
  back as a FAILED `WalkResult` whose `error` says it is out of range.

### Avoid areas and route links (native ABI 7, WorldWalker #44)

`avoid=[W.Area(...), ...]` (on every method above, up to 64 areas) gives tile rectangles the
walker never routes through: no walk step lands on them, no door, boat or teleport is taken that
starts or lands in one, and the walk clicks round them a step at a time when near. An area holding
the start or the goal is lifted for that plan. A route that can only cross one fails with no
route; it is never quietly taken through it. `nav.route(goal)` plans once and returns a `Route`:
its `steps` and, in order, the transports and teleports it takes (`links`, each a `RouteLink`
with its `kind`, `origin`, `object_id`, `from_tile`, `to_tile`, fairy-ring `code` and teleport
`name`).

```python
from botwithus.core import walk as W

guards = W.Area.around(3222, 3218, 4, plane=0)        # or W.Area.box(x1, y1, x2, y2, plane)
r = nav.walk_ex(goal, avoid=[guards])
route = nav.route(goal, avoid=[guards])
for link in route.links:
    print(link.kind, link.name or link.object_id, link.to_tile)
```

Areas are refused, never ignored: on a bwu_host older than ABI 7 with `NativeUnavailable`
(`NativeHost.has_avoid` is False), and on a worldwalker.dll older than WorldWalker #44 with
`WalkOptionsUnsupported`, before anything moves. Without areas the call is the one made before.

## Cache definitions and the world map (native ABI 7)

The native host reads the game cache through NXTCache.dll and hands it over typed:

```python
from botwithus.api.definitions import CACHE_STRUCT, Definitions, item_name
from botwithus.api import worldmap

defs = Definitions()
whip = defs.item(4151)          # ItemDef: name, inventory_ops, ground_ops, stackable, noted, ...
defs.npc(0).name, defs.loc(1276).ops
defs.params(CACHE_STRUCT, 4001) # [Param], ascending param id
defs.enum(1234)                 # the long tail as NXTCache's JSON: enum, struct, param,
                                # varbit, inv, quest, json(CACHE_WORLDMAP, id)
item_name(1512)                 # "Logs": a note reads as its item, as the game shows it

worldmap.elements()                                   # area 28, the main world, by default
worldmap.elements(category=2613, near=(3233, 3222), radius=10)   # the Lumbridge lodestone
worldmap.elements(231)                                # Prifddinas at its real tiles
```

- A missing id is `None`; no cache (or an NXTCache.dll older than the read) raises
  `CacheUnavailable`; a bwu_host older than ABI 7 raises `NativeUnavailable`.
- **World map areas.** Every element belongs to an area, and its tile is in that area's map
  space. Area 28 draws a few regions (Prifddinas, Ashdale) at a display position and places them
  again, at the real tile, in their own area: the Prifddinas lodestone is (4576, 2665) in 28 and
  (2208, 3361) in 231. So `elements()` reads area 28 unless told otherwise, as the Java host
  does. Many definitions have no name (the lodestones among them): match by `category` and tile.

## Varps and varbits: decide on the state, not the value

Varps are set lazily by the server, so most varps have no client-side entry at all, and a
value alone can't tell you whether one is set: a set varp can hold `-1`, and so can a varp at
its default. Every read carries a `VarpState`:

```python
from botwithus.api.variables import VarpState

r = ctx.game.read_varp(3)            # or read_varps / read_varbit / read_varbits
match r.state:
    case VarpState.SET: ...                          # r.value is the stored value
    case VarpState.DEFAULT_NOT_SET_CLIENTSIDE: ...   # r.value is the default the game reads
    case VarpState.NO_SUCH_VARP: ...                 # the cache has no such varp
    case VarpState.UNAVAILABLE: ...                  # not in the world yet, bad id, timeout
```

- `varp(id)` / `varps(ids)` / `varbit(id)` return what the game reads: the stored value, the
  default when unset, or `-1` for no value. `varp_long(id)` gives all 64 bits of a LONG varp,
  whose `value` is only the low 32.
- Defaults come from the game cache through the native host. If it cannot confirm one (an
  older `NXTCache.dll`, or the cache still warming up just after attach), the read is still
  `DEFAULT_NOT_SET_CLIENTSIDE`, with `value` `0` and `default_verified` false.
- A varbit takes its base variable's state and decodes the base's value, so a varbit over an
  unset base reads the base's default bits, exactly as the game does.
- In the lobby and for the first few ticks after entering the world, every read is
  `UNAVAILABLE`.

## Plugin UI: native game components

`ctx.ui` makes components that the game client draws itself, with its own z-order, scaling and
styling. They are the components Jagex's own Lua plugins make. This is an extra, opt-in UI
route. It does not change settings, run logs or anything else.

```python
from botwithus.api import UiEvent, rgba
from botwithus.api.ui import UiRefused

root = ctx.ui.component(1477, 61)                       # a Jagex parent: TOPLEVEL_V2__GAME_AREA
panel = ctx.ui.layer(root, x=40, y=40, width=320, height=140)
label = ctx.ui.text(panel, "Logs: 0", x=8, y=8, width=300, height=20, rgba=rgba(255, 255, 0))
box = ctx.ui.rectangle(panel, x=8, y=32, width=20, height=20, fill=True, rgba=0x00FF00FF)
canvas = ctx.ui.canvas(root, x=40, y=190, width=320, height=100)
with canvas.draw() as d:                                # one call for the whole batch
    d.rect(0, 0, 320, 100, rgba=0x20202080)
    d.line(10, 10, 310, 90, thickness=3, rgba=0xFF0000FF)

label.set(text="Logs: 1")                               # any prop; all or nothing
print(label.get(content=True).content)                  # readback: geometry, colour, text...
ctx.events.subscribe(UiEvent, lambda e: ctx.log.info("%s %s %s", e.handle, e.hook, e.reason))
```

- **Types:** `layer`, `text`, `rectangle`, `line` and `canvas`, and the interactive types below.
  `ctx.ui.create(parent, "<type>", **props)` takes any type name. The agent refuses a type it
  does not support yet, such as `sprite`, with `UiRefused("ui type not supported yet: sprite")`.
  A later type then works with no framework change.
- **Errors arrive as the host raises them.** `UiRefused`, a `RuntimeError`, carries the
  agent's refusal text verbatim. `ValueError` is a refusal the host makes without sending: a
  forbidden prop (`op_config`, `drag_config`, `target`, ...), category 0, or an edit of a
  Jagex component. `TimeoutError("rpc_timeout")` means the outcome is unknown. A Jagex handle
  from `ctx.ui.component` is read-only: you can use it as a parent and with `get()`, nothing
  else.
- **Events.** `UiEvent(handle, hook, reason, fields)` comes through `ctx.events.subscribe`.
  It is delivered only to the script that made the component, or, for a Jagex component, to
  every script that looked it up: they share one handle. `ctx.ui.subscribe(fn, handle=...,
  hook=...)` filters by handle and hook (locally: it subscribes nothing on the agent). The
  interactive hooks are below; `destroyed` needs no subscribe and has a `reason`:
  `parent_gone` (for example, on logout), `removed`, `server_events` or
  `interface_unreadable`. The synthetic `overflow` event (`handle == 0`, `dropped`) means
  events were lost. Handlers run on the client's own ui-events thread; keep them cheap.
- **Canvas batches.** `canvas.draw()` sends its ops in chunks of 128, one call each. Each chunk
  is all or nothing, but **a batch of more than 128 ops is not atomic**: if a later chunk is
  refused, the earlier ones stay drawn, and `batch.sent` says how many ops were applied.
- `ctx.ui.handles()` lists every handle on the script's connection (`ui.list`).
- **Explicit destroy and children.** `component.destroy()` takes its children with it in the
  game, but the agent sends no `destroyed` for an explicit destroy (PUBLIC_PROTOCOL 7.3), and
  the framework does not track parentage. A child's `.alive` therefore stays True; a call on it
  is refused with `UiRefused` "unknown handle", and the script's release skips it.
- **After the script stops,** every `ctx.ui` call raises `RuntimeError("ui: this script has
  stopped")`, for instance from a thread the script started. A create already in flight when
  the script stopped is destroyed as soon as it returns.
- **Lifetime.** When a script stops, after `on_stop` and also after a crash, the framework
  destroys exactly the components that script made. Other scripts on the same client keep
  theirs. The native host's release on detach is the backstop, and so is the agent's when the
  connection closes.

### Interactive components (P2)

Needs an agent from NXTLibrary `a17edc4` and a `bwu_host` from native-scripting-host `7bb38a4`
or later (`ctx.ui.has_p2`; an older agent refuses the types verbatim).

```python
from botwithus.api import UiButtonPressed, UiEntry, UiSelectionChanged

box = ctx.ui.panel(root, vertical=True, x=40, y=40, width=220, height=260)
go = ctx.ui.button(box, "Start", can_toggle=True, width=200, height=30)
done = ctx.ui.check(box, checked=False, width=30, height=30)
speed = ctx.ui.slider(box, min=0, max=100, step=10, ticks=10, value=20, width=200, height=30)
mode = ctx.ui.combo_field(box, [UiEntry(1, "Fast"), UiEntry(2, "Safe")], selected=1)
trees = ctx.ui.list(box, [(1, "Oak"), (2, "Willow"), (3, "Yew")], selection_limit=3)
name = ctx.ui.input_field(box, max_length=20, filter="alphanumeric")

go.on_pressed(lambda e: ctx.log.info("pressed %s", e.current))            # UiButtonPressed
speed.on_changed(lambda e: ctx.log.info("speed %d", e.value))             # UiSliderChanged
trees.on_selection_changed(lambda e: ctx.log.info("%d %s", e.entry_id, e.reason))
off = name.on_content_changed(lambda e: ctx.log.info("%s %r", e.reason, e.content))
ctx.events.subscribe(UiSelectionChanged, on_pick)   # by class: slider and entry changes both
print(trees.read_selected(), speed.read_value(), name.get(content=True).renders)
name.blur()                                     # give the game its keys back
off()                                           # unsubscribes once its last handler goes
```

- **Helpers and props.** `button(parent, label, ...)` (`label`, `can_toggle`, `toggled`),
  `check(parent, checked, ...)`, `input_field(parent, content, ...)` (`content`, `filter`,
  `max_length`), `slider(parent, min=, max=, step=, ticks=, value=)` (the four range props go
  together), `combo_field(parent, entries, selected=)` (one id, -1 none),
  `list(parent, entries, selected=, selection_limit=)` (`selected` is the ids that end up
  selected), `panel(parent, vertical)` and `grid(parent)`. `enabled` works on every agent
  component. Entries are `UiEntry(id, text)`, `(id, text)` or `{"id", "text"}`. The name
  `list` is the list component; `ctx.ui.handles()` is the listing. The agent refuses a check
  `label`, panel `spacing`, grid layout props and an input `vis_mode` as "not supported yet".
- **Readback.** `get()` adds each type's own keys and leaves the rest None: `enabled`; button
  `label`, `can_toggle`, `toggled`; check `checked`; input `focused`, `filter`, `max_length`,
  `font_id`, `sprite_id`, `renders`; slider `min`, `max`, `value`, `step`, `ticks`; combo and
  list `entries` (`UiEntryRead`); combo `selected` (an id); list `selected` (a tuple) and
  `selection_limit`; panel `vertical`. **On 951-4 an agent input_field may not draw** until the
  agent finds a background sprite to copy (`renders` False, which is common in the world).
  Content, events and focus work regardless; the framework reports `renders` as it is.
- **Hooks**, subscribed on the agent per component: `component.on(hook, fn)` returns `off()`;
  the typed helpers are `on_click`, `on_release`, `on_mouse_over`, `on_mouse_leave` (any
  type), `on_pressed` (button, check), `on_content_changed` (input_field), `on_changed`
  (slider) and `on_selection_changed` (combo, list). `subscribe_hook(hook)` and
  `unsubscribe_hook(hook) -> bool` are the bare verbs; `hooks()` says what is subscribed.
- **Typed events**, all `UiEvent` subclasses, so `ctx.events.subscribe(UiEvent, fn)` still
  gets everything: `UiClick` / `UiRelease` / `UiMouseOver` / `UiMouseLeave` (`x`, `y`),
  `UiButtonPressed` (`current`, `previous`), `UiContentChanged` (`reason`, `reason_code`,
  `content`, `is_submit`), `UiSliderChanged` (`value`) and `UiEntrySelectionChanged`
  (`entry_id`, `selection_event`, `selected`) under `UiSelectionChanged`. A known hook the
  host could not decode arrives as a plain `UiEvent` with its raw `fields`; the typed helpers
  never hand one to their handler.
- **A set never raises the change hook** (a slider value, a combo or list selection): only the
  user's input does. The exception is an input_field `content` set, which raises
  `UiContentChanged` like typing. **The client raises it after `set` returns**, on its game
  thread, so a content set made just before subscribing can still arrive once the
  subscription is in place (seen live on 951-4). Tell your own sets from typing by context.
- **Handlers stop with the script.** Once a stop is requested, no ui event reaches the
  script's handlers or `ctx.events`, and the framework waits for a handler still running
  before it calls `on_stop`. An event for a component the script destroyed is not delivered
  either, even if it was already queued, and a handler that `off()` removed is never called
  again.
- **Unsubscribing drops events still queued.** When `off()` removes a hook's last handler, or
  you call `unsubscribe_hook`, and the agent answers, the host discards that hook's events it
  had not handed over yet. So a click made just before you unsubscribe may never reach a
  handler. Events the ui event thread had already polled are not dropped. They skip a handler
  that `off()` removed, but still reach `ctx.ui.subscribe(...)` handlers, `ctx.events`, and any
  `on()` handler still registered (`unsubscribe_hook` alone removes none).
- **Focus.** While an input_field is focused (a user clicked it), the game's keys go to it.
  `blur()` returns whether it had focus.
- **Subscriptions go with the component.** A destroy, a `destroyed` event or the script's stop
  takes them, and the framework sends no `ui.unsubscribe` for them. Unsubscribing a component
  that is already gone returns False and sends nothing.

## Interacting: components, inventories, dialogs

```python
from botwithus.api import draw
from botwithus.api.dialog import Dialog

game = ctx.game
game.interact_component(1473, 5, "Drop", sub=3)      # an op by the name the menu shows
pack = game.backpack()                               # also game.bank(), game.equipment()
pack.interact_item(1511, "Drop")                     # first slot holding item 1511
if pack.count_named("logs") > 20: ...                 # names from the cache by default
dialog = Dialog(game)
if dialog.is_open():
    dialog.select("What's wrong?")                   # or select(2); continue_chat() for 1184
game.draw.highlight(draw.highlight_npc(npc.server_index, label="target"))
```

- **Ops are read live** (`get_component_ops`), never derived from a cache option index: on
  the backpack the two differ (Drop is cache index 5 but op 8 on the bronze arrow).
- **Item names come from the cache.** Neither the snapshot nor any agent RPC carries an item's
  name, so every match by ITEM name resolves ids through `names=`, which defaults to the cache's
  item names (`botwithus.api.definitions.item_name`, native ABI 7). Pass your own callable to
  match on something else. Option names ("Drop") need none.
- **Debug draw is owned by the pipe connection.** Scripts that share one `Connection` share a
  key space, and `game.draw.clear_all()` removes all of their drawings. Every command carries a
  TTL (3 s by default), so drawings fade after a script stops; the framework does not clear them.

## Composition helpers: waits, areas, banking, combat, quests, HUD

The same helpers the Java host has (its phases J1-J3), with the same names in snake_case.

```python
import re
from botwithus.api import RectangularArea, Npcs, Prayer, HudTab, Lodestone, Skill, XpTracker

game = ctx.game
ctx.delay_until(lambda: game.bank().is_open(), 5_000)   # False on timeout or stop; holds while paused
ctx.delay_ticks(2, 4)                                   # server ticks, never game cycles
yard = RectangularArea(3200, 3200, 3215, 3215)
me = ctx.self_player.tile
goblin = (Npcs(ctx.snapshot()).inside(yard).not_animating()
          .with_health_percent(50, 100).nearest_to(me))
bank = game.bank()
bank.open()                                             # nearest booth/chest, else a banker
bank.deposit_all_except("Knife", re.compile(r".* hatchet"))   # WHOLE names; a Pattern must match it all
bank.withdraw("Lobster")                               # one, by whole name
game.prayers().toggle(Prayer.PROTECT_MAGIC)             # the prayer window must be open
game.action_bar().cooldown("Backhand")                  # client cycles (20 ms); UNKNOWN if the scripts fail
game.quests().status(257).stage                         # NOT_STARTED / STARTED / COMPLETE / UNKNOWN
game.hud().open(HudTab.EQUIPMENT)
game.lodestones().teleport(Lodestone.BURTHORPE)         # blocks until arrived and settled
xp = XpTracker(game, Skill.WOODCUTTING); xp.xp_per_hour()
ctx.pause(); ctx.resume()                               # on_loop and the waits hold while paused
game.gamevals().id(GamevalType.VARBIT, "ACTIONBAR1_INT1_TYPE")   # names -> ids (native ABI 8)
game.varbit_by_gameval("COMBATV2_ACTIONBAR_CURRENT_BAR")         # -1 when the name is unknown
ctx.settings.put("route", "north"); ctx.settings.save()          # kept by the GUI for the next run
```

- **Whole names.** The new name-taking inventory and bank methods (`matching`, `first_exact`,
  `contains_any_of`, `deposit_all`, `withdraw`, `interact_first`, ...) match the WHOLE item name
  ignoring case, and a compiled `re.Pattern` must match the whole name. The older `*_named`
  methods still match a substring. These decide what gets clicked: "Logs" must not deposit Oak logs.
- **`botwithus.api.Area` is still the walker's avoid area.** The shapes for testing positions are
  `RectangularArea`, `CircularArea` and `PolygonalArea` (`botwithus.api.world`);
  `RectangularArea.to_avoid()` converts one.
- **Gameval names.** The api modules address their interfaces, components and variables by the
  same gameval names as the Java host (`BANK__BANK_INV`, `ACTIONBAR3_INT5_TYPE`, ...), resolved
  through the native host's index (`botwithus.api.gameval`, native ABI 8): the game's own
  index-67 tables in the local game cache, read without the network. Each name carries the id it
  had when the module was written; when the name does not resolve (an older host, no local cache)
  that id is used and one warning per name says so. The action-bar fallback table is generated by
  `tools/gamevals/gen_action_bar_ids.py`, and a bar falls back whole, never name by name.
- **Derived events.** `InventoryChanged` and `SkillChanged` are published once per server tick,
  diffed from the snapshot. An inventory that appears (the bank opening) is a silent baseline.
- **`ctx.settings`** is a writable string map merged with the GUI's settings. In a GUI worker
  (native ABI 8) `settings.save()` hands it to the GUI's store, beside the run's saved config, and
  the next run starts from it with the config merged over it. Declared config keys are owned by
  the config, so a `put` on one lasts for the run only. Under `bwu_host --py` there is no store
  and `save()` returns False.
- **Pause from the GUI.** In a GUI worker (native ABI 8) the GUI's pause button holds the script
  exactly as `ctx.pause()` does, and the GUI shows a pause the script made itself.

## Script protection (honest statement)

This framework loads scripts through a pluggable artifact abstraction (plain `.py`
directory, entitlement-gated bundle, or native-compiled `.pyd`). Only plain `.py` scripts
load today; the other two sources are stubs. Python source and bytecode are not protected:
treat any `.py` script you hand out as readable by whoever runs it.

## Requirements

- The BotWithUs native host (`bwu_host.exe`), installed by the BotWithUs launcher. It brings
  its own Python 3.12; you do not install Python to run scripts.
- A running game client with the BotWithUs agent injected.

## Developing the framework

The authoritative wire spec is `NXTLibrary/docs/PUBLIC_PROTOCOL.md`.

`src/botwithus/core/layout.py` (`PROTOCOL_VERSION`, the snapshot offset table and the event
discriminators) mirrors the agent's `NXTLibrary/src/ipc/SharedLayout.h` and `Events.h`. It
decodes the raw snapshot and event **bytes** the native host returns, so any change to the
agent's shared-memory layout must land here in lockstep (the native host self-pins its own
vendored copy). The RPC method surface evolves additively and is not version-gated.

To run a working copy of the framework under a developer build of the host, put `src` on
`PYTHONPATH`; a developer build (no embedded runtime beside it) uses CPython's normal
discovery:

```powershell
$env:PYTHONPATH = "path\to\python-scripting-framework\src"
bwu_host.exe --py your_script.py
```

The unit suite runs on any plain Python 3.11+ with no client or host. It injects a fake
native backend (`tests/fakes.py`):

```powershell
python -m pip install -e .[dev]
python -m pytest
```

CI runs the same suite on Python 3.12 (the runtime `bwu_host` ships) and 3.11 (the floor).
`CONTEXT.md` is the maintainer's orientation, including the native-host version history
behind some of the behaviour described here.

## License

Proprietary. Copyright (c) 2026 BotWithUs. All rights reserved. See `LICENSE`.
