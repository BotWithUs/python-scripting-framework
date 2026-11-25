# Changelog

## 1.1.0 — 2026-11-21

- **Docs: unsubscribing drops events still queued (no code change).** The README and the
  `Component.unsubscribe_hook` / `Component.on` docstrings now say that an answered
  `ui.unsubscribe` makes the native host discard that hook's events not handed over yet, so a
  click made just before `off()` or `unsubscribe_hook` may be lost. They also say which handlers
  still get an event the ui event thread had already polled.

- **Plugin UI P2: interactive components in `ctx.ui`** (NXTLibrary `a17edc4`, native-scripting-host
  `7bb38a4`; detected by `_native.ui_subscribe`, not the ABI version; no protocol change):
  - Helpers and wrappers `button` / `Button`, `check` / `Check`, `input_field` / `InputField`,
    `slider` / `Slider`, `combo_field` / `ComboField`, `list` / `ListBox`, `panel` / `Panel` and
    `grid` / `Grid`; `ComponentType` gains the eight names; `UiEntry` and `UiEntryRead`.
  - `UiReadback` reads the P2 keys where the type has them (None otherwise), `renders` included
    as the agent reports it.
  - `Component.subscribe_hook` / `unsubscribe_hook` / `hooks` / `on(hook, fn)` and typed
    `on_*` helpers. Subscriptions are per script and go with the component: no `ui.unsubscribe`
    is sent for a dropped handle, at destroy or at the stop's release.
  - Typed events: `UiClick`, `UiRelease`, `UiMouseOver`, `UiMouseLeave` (`UiMouseEvent`),
    `UiButtonPressed`, `UiContentChanged`, `UiSliderChanged` and `UiEntrySelectionChanged`
    (`UiSelectionChanged`), plus `UiContentReason`, `UiSelectionEvent` and the hook names on
    `UiHook`. `ctx.events` dispatches a ui event to its class and every base class.
  - `InputField.blur()`.
  - Tests: `test_plugin_ui_p2.py`; live tooling `tests/live/ui_fake_agent_check_p2.py`,
    `ui_smoke_p2.py` and `ui_smoke_run.py --mode p2`. The P1 checks that expected `button` to be
    refused now use `sprite`.

- **Native host ABI 8: gameval names, pause from the GUI, a settings store.**
  - `botwithus.api.gameval`: `GamevalType` (the Java wire strings), `GamevalIndex` (`id`,
    `require`, `gameval`, `starting_with`, `component`, `interface_id`, `is_available`,
    `index_version`) over `_native.gameval_*`, `GamevalRef(type, name, fallback_id).resolve()`.
    `GameApi.gamevals()`, `varp_by_gameval` / `varp_long_by_gameval` / `varbit_by_gameval` /
    `varc_int_by_gameval` (-1 when the name does not resolve, as Java's `getVarp(String)`).
  - Every pinned Java `GamevalRef` fallback id now resolves by its name first: the action bar
    (slot variables all of one bar or the generated table, slot buttons, current bar, global
    cooldown varcs, `COMBATV2_TYPE_TO_ENUM`), prayers (73 struct and varbit names, points, mode,
    quick prayers, window, buttons, ribbon), combat, HUD, bank, buffs, summoning, lodestones,
    chat pages, item destruction, deposit box and loot. A name that does not resolve uses the old
    id and logs one warning. Quests are listed from the gameval index (the id scan is the
    fallback). The modules take an optional `gamevals=` index.
  - Pause: in a GUI worker, `ctx.pause()` / `ctx.resume()` go to the host and the GUI's pause
    button comes back through a watcher thread (`core.ctl.PauseWatcher`, `ctl_wait_pause`).
  - Settings: `ctx.settings` starts from the map the last run saved, with the config merged over
    it; `save()` sends it to the GUI's store (`ctl_save_settings`) and logs a refusal.
  - Needs native ABI 8; on an older host the names fall back to the old ids, `save()` returns
    False and the GUI cannot pause the run.

- **Composition APIs ported from the Java host (JBotWithUsV2 J1-J3, develop 57449b45)**. No
  native ABI change; everything is composed on reads the frontend already had:
  - Waits (`botwithus.api.timing`): `delay_until`, `delay_while`, `delay_until_ticks`,
    `delay_ticks` (also on `ScriptContext`), stop-aware within one 50 ms poll and held while the
    script is paused; `Stopwatch`, `Timer`, `format_duration`, `per_hour`.
  - `Coordinate`, `RectangularArea`, `CircularArea`, `PolygonalArea` (`botwithus.api.world`):
    contains, random tile, random reachable tile (`SceneWalker.is_reachable`).
  - `Skill` gains `display_name`, `is_elite`, `xp_table`, `by_id` (`SkillType` is the same
    enum); `XpTable` (standard to 126, elite to 120; the caller passes the cap) and `XpTracker`.
  - Inventories: whole-name / Pattern / category / option reads and `interact_first`;
    `Backpack.use`. `game.bank()` is now a `Bank` (`botwithus.api.banking`): open (booth, chest or
    banker), deposit / withdraw by id, name or Pattern, `deposit_all(*items)`,
    `deposit_all_except`, presets, `load_last_preset`, `set_withdraw_mode`, `set_transfer_mode`.
  - Entity queries: `with_ids`, `named_any_of`, `with_option`, `inside` / `outside`, `on_plane`,
    `reachable`, animation / stance / spot-anim / moving / combat-level / health filters, and
    `last`, `random`, `nearest_to`. Names and options come from the cache
    (`botwithus.api.interact`, which also clicks an entity option by name).
  - `game.action_bar()` (slots, abilities from the cache, cooldowns joined through client scripts
    8247 then 6506, in client cycles), `game.prayers()`, `game.combat()` (+ `HitmarkHistory`),
    `game.summoning()` (beast of burden inv 530), `game.lodestones()`.
  - Events `InventoryChanged` and `SkillChanged`, derived once per server tick.
  - `game.components(iface, comp)` (`ComponentQuery` / `ComponentNode`), `Dialog.get_text` /
    `get_title` / `continue_dialog` across 1184/1191/1186/1189, `ConfirmItemDestruction`,
    `game.quests()` (`QuestLog`; stub records read their listed twin), `game.deposit_box()`,
    `game.loot()`, `game.buffs()`, `game.hud()`, `game.logout()`, `game.exit_to_lobby()`.
  - `ScriptContext.pause()` / `resume()` / `is_paused`, and `ctx.settings` (not persisted).
  - Tests: `test_j1_*.py`, `test_j2_*.py`, `test_j3_*.py`.

- **Cache definitions, the world map, avoid areas and route links** (native ABI 7; needs a
  bwu_host with `BWU_ABI_VERSION` 7, else `NativeUnavailable`):
  - `botwithus.api.definitions`: `Definitions().item / npc / loc` as `ItemDef` / `NpcDef` /
    `LocDef` (names, ops, stackable, noted, members, category, ...), `params(type, id)` as
    `Param`, `int_param`, and the long tail as NXTCache's JSON (`json`, `enum`, `struct`, `param`,
    `varbit`, `inv`, `quest`, `CACHE_WORLDMAP`). A missing id is `None`; no cache raises
    `CacheUnavailable`. `item_name(id)` keeps names on this side.
  - The inventory's by-name matching (`all_named`, `first_named`, `contains_named`,
    `count_named`, `interact_named`) now defaults `names=` to the cache's item names. A
    non-callable `names=` is still a `TypeError`.
  - `botwithus.api.worldmap`: `elements(area_id=28, *, name, category, near, radius, plane)` and
    `nearest(...)` as `MapElement`, the main world area by default, `area_id` kept.
  - `walk.Area` and `avoid=[...]` on `SceneWalker.plan / walk / walk_ex / start / walk_to` and
    `NativeHost.path / walk / walk_ex / walk_start`; `SceneWalker.route(goal)` /
    `NativeHost.path_route` return a `Route` (steps and `RouteLink`s). A worldwalker.dll older
    than WorldWalker #44 raises `WalkOptionsUnsupported`; no areas keeps the old call shape.
  - Tests: `test_n2_definitions.py`, `test_n2_worldmap.py`, `test_n2_avoid.py`.

- **Client reads of NXTLibrary 0f746b4** (over `GameApi.call`, no host ABI needed; an older agent
  refuses them as unknown methods):
  - `game.ge_offers()`: the eight GE slots of market 0 as `GeOffer` (`in_use` is `stage != 0`;
    `price` and `gold_from_sale` can exceed 32 bits).
  - `game.world_list()`: a `WorldList` of `World` rows with `members`, `skill_total`,
    `classic_only` and `eoc_only` decoded from the flags (other bits raw). An empty list
    (`is_fetched` False) means the game has not fetched it yet, not an error.
  - `game.set_world(id)` and `game.hop_world(id, timeout_s=30)`: set_world, then poll
    `get_current_world` and `get_login_state` until the world is reported with the state back at
    20 or 30. Returns a `HopResult` (`arrived`, `started`, `reason`, ...); a refusal does not
    raise. A GUI stop ends the wait.
  - `game.target_mode()` and use-on: `use_on_component`, `use_on_npc`, `use_on_object` (any
    `ComponentRef` source) and `use_item_on_item` / `use_item_on_npc` / `use_item_on_object`
    (backpack slots). Each arms with SELECT_COMPONENT (25), waits until target mode reads active
    with that source, then queues 58, 8 or 2; False, with no target action queued, when it never
    arms. Ground items, players and tiles are not bound: their packing is not documented.
    `ActionType` gains `SELECT_COMPONENT`, `SELECT_COMPONENT_ITEM`, `SELECT_NPC`, `SELECT_OBJECT`.
  - `game.world_to_screen(tiles)`: 1..64 tiles to window pixels as a `Projection`
    (`ScreenPoint.on_screen` is the predicate per point).
  - `game.static_children_with_sub` / `dynamic_children_with_sub` (and
    `InterfaceComponent.dynamic_children_with_sub`): children as `Child(comp, sub)`. Use these
    instead of the flat `dynamic_children`, whose entries all repeat the parent's id.
  - `game.execute_script(..., string_args=[...])`: string arguments, at most 16 args together
    and 255 UTF-8 bytes each, refused with `ValueError` before sending.
- **Debug draw and highlights:** `game.draw` over the agent's `debug_draw_*` and `highlight_*`
  RPCs, with builders in `botwithus.api.draw` (line, rect, ellipse, text, poly, component box;
  highlights of an NPC, a player, yourself, a tile, an area or a component). Keyed, TTL always
  sent, limits checked before sending. Drawings are owned by the pipe connection, so scripts
  sharing one `Connection` share keys and `clear_all`.
- **Component ops and clicks by name:** `game.component_ops(iface, comp, sub)`,
  `static_children`, `dynamic_children` (a count only: see its docstring),
  `game.interface_component(...).interact("Drop")` and `game.interact_component(...)`.
- **Inventory clicks:** `game.backpack()`, `game.bank()`, `game.equipment()` return an
  `InventoryContainer` that clicks by slot, item id or item name, with the option as an op
  number or the name the menu shows (read live). Matching an ITEM by name takes a `names=`
  resolver: the Python frontend has no item definitions. `InventoryView` gains `present`,
  `slot`, and `count_named` / `contains_named` / `first_named` / `all_named`.
- **Dialogs:** `botwithus.api.dialog.Dialog`: `options`, `is_open`, `select(text | index)`,
  `is_chat_open`, `continue_chat`, as the Java host's `Dialog`.
- **Obj vars:** `game.obj_vars(inv_id, slot=None)`, `game.slot_obj_vars`, and the
  `ObjVarChanged` event (event 13), which used to be skipped.
- **World spot anims:** `game.spot_anims()` (`query_spot_anims`).
- **Action queue history:** `game.action_history(max_results, action_id)` (oldest first: a
  cap keeps the OLDEST entries) and `game.last_action_time()`.
- **`KeyInput` carries the agent's key flags:** `synthetic`, `text_input_focused` and `is_repeat`,
  decoded from the body's last byte (NXTLibrary PR #84, merged at `ee221d2`). They default to
  `False`, so existing positional construction still works, and against an older agent they all
  read `False`. `text_input_focused` is set when a text field (chat, bank search, a prompt, a native
  input) had the keyboard, so scripts can ignore keys typed into text. A `KeyInput` body shorter than 8 bytes
  is now skipped with a debug log. It used to raise out of `poll()`.

## 1.0.0 — 2026-11-08

Scripts can now run in the `bwu_host` GUI. The GUI lists the scripts in
`~/.botwithus/scripts/python`, shows their settings, and runs each one in its own worker
process. `bwu_host --py <file>` still runs a file directly.

### Breaking changes

- **(a) The `script_manifest` `author` defaults to `""`, not `"Unknown"`.** This matches the
  Java host. The GUI shows an empty author as "Unknown author". A script that relied on the
  text `"Unknown"` must set it explicitly.
- **(b) `on_config_update` is called before `on_start` under `bwu_host --py` too.** It
  receives every field's default. Before, CLI runs never called it. A script that
  configures itself in `on_config_update` now behaves the same under `--py` and in the GUI.
- **(c) Starting a script with an invalid manifest or config fields raises
  `ConfigSchemaError`.** `Connection.start_script` / `ScriptRunner.start` raise it, start
  nothing and register nothing. The message is `"<key path>: <detail>"`, for example
  `config_schema[0].default: ...`.
- **(d) `get_config_fields()` is now validated, as strictly as the native host does.**
  Under `--py` the framework checks it with the host's own `ctl_describe` (its own validator
  is the fallback), and the GUI checks it when the script is listed. Native is stricter than
  the Java host in four places:
  - a `choice` default must be one of its options;
  - a `choice` needs at least one option;
  - an `item_id` default cannot be below -1;
  - a `string` default longer than 256 bytes needs an explicit `max_len`.
- **(e) `ScriptRunner.stop()` on a runner that has already finished leaves it `STOPPED`.**
  Before, it was left at `STOPPING` (a bug). A crashed runner stays `ERROR`.
- **(f) `ctx.is_stop_requested()` also reads the GUI's stop flag.** In a GUI worker it reads
  `ctl_stopping()`, so it turns true when the GUI stops the script, not only on
  `stop_self()` or the runner's stop. `ctx.sleep` and `ctx.sleep_ticks` return `False`
  once that happens. Under `--py` nothing changes.
- **(g) `queue_actions` and `queue_action` raise typed errors.** The agent now queues a
  batch whole or refuses it whole. A refusal (`queue_full`, `batch_too_large`,
  `action_id_required`, `bad_param`) raises `ActionsRefused`, which is the only error that
  means nothing was queued. A host timeout raises `ActionOutcomeUnknown`. Both are
  `RpcError`s, so code that caught `RpcError` keeps working.
- **(h) The `botwithus` console command is gone,** and so is the unused `gui` extra.
  `python -m botwithus.host` now exits with code 2 and says to run scripts through
  `bwu_host --py <script>`.
- **(i) An RPC after the client exits raises `ClientExited`** (a `ConnectionError`), not
  `RpcError`, and `sleep` / `sleep_ticks` return `False` on a client exit. Code that caught
  `RpcError` to outlast an exit no longer catches it. `worker.CLIENT_GONE_CONFIRM_S` moved to
  `botwithus.core.client_exit.CLIENT_GONE_CONFIRM_S`. Details under Fixed.

### Added

- `botwithus.worker.describe(path)` / `run(path, client_pid)`: the entry points the native
  host calls for the GUI (`docs/GUI-WORKER-PROTOCOL.md`, native-scripting-host).
- Config fields: `int_field`, `string_field`, `bool_field`, `choice_field` and
  `item_id_field`, plus `ConfigSchemaError`. Also `ctx.config`, a read-only mapping of the
  values in force.
- `script_manifest(category=...)`, which groups the script in the GUI.
- `ctx.stopping`. Also `botwithus.api.stop_requested()` and `StopRequested`, for
  self-running scripts.
- `botwithus.core.native.default_client_pid()`. It picks an explicit pid first; then, in a
  GUI worker, the client the user chose (`BWU_CLIENT_PID`); then the first client found.
- In a GUI worker, `LaunchHandle.outcome()` raises `StopRequested` when the GUI stops the
  script. `Subscription.poll()` returns `None` then, and iterating a `Subscription` ends.
  Under `--py` none of this changes.
- The `ActionRefused` event (type 41): the agent tells a script that it refused an action,
  why (`RefuseReason`) and where it was waiting (`AnchorCase`). Unknown values stay plain
  integers.
- `ActionsRefused` and `ActionOutcomeUnknown`, and `game.MAX_ACTION_BATCH = 128`.
- `botwithus.api` re-exports the names a script author needs (`BotScript`,
  `script_manifest`, `ScriptContext`, the event types, `Tile`, `GameApi`, the action
  builders and the errors).
- The package is typed (`py.typed`).
- A `LICENSE`: the framework is proprietary. You may use it to write and run scripts with
  the BotWithUs native host; it ships only inside the native host package.

### Fixed

- **`walk_wait` no longer wraps a long timeout.** A timeout of 2**32 ms or more used to be
  cut down modulo 2**32, so `wait(2**32)` returned at once. It is now clamped, and the top
  value (`WALK_WAIT_FOREVER`, also what `float("inf")` maps to) waits until the walk ends.
- **A GUI worker run now ends when its game client exits.** The worker asks the native host
  to `refresh` the snapshot instead of reading it: after an exit the host keeps returning the
  last frame, so a read never failed and the run could keep going on frozen data. A run now
  ends `stopped`, through `on_stop`, once `refresh` fails **and** two process discoveries at
  least 1 s apart both miss the client. A refresh failure while the client is still listed,
  or one discovery miss, does not end the run. `on_stop` can still read the last frame.
  New: `NativeHost.refresh()`.
- **`ctx.sleep_ticks` and `ctx.sleep` now return `False` about 1 s after the client exits.**
  They check the same exit rule while they wait (`refresh` fails, and two discoveries at
  least 1 s apart both miss the client). Before, `sleep_ticks` waited out its whole timeout
  (6 s by default), because after an exit the snapshot keeps returning the last frame and
  `server_tick` stops moving. A tick stall while the client is still up times out as before,
  and the wait only checks discovery after `refresh` has failed. The rule now lives in
  `botwithus.core.client_exit` (`ClientExitWatch`, `CLIENT_GONE_CONFIRM_S`). It moved there
  from `botwithus.worker`, so the worker loop and the waits use the same rule. A first miss
  older than twice the confirm time is dropped, so a stale miss cannot pair with a later one.
- **An RPC after the client exits now marks the host dead.** The native host fails these
  calls with "client exited". They now raise `botwithus.core.native.ClientExited`, a
  `ConnectionError`, instead of a plain `RpcError`, and `conn.alive` turns false. If
  `ClientExited` escapes a script callback, the script ends `stopped`, not crashed. In a GUI
  worker that is `on_config_update` (the initial call and queued changes), `on_start`,
  `on_loop` and `on_stop`; under `bwu_host --py` it is the initial `on_config_update`,
  `on_start`, `on_loop` and `on_stop`. A queued change that fails this way is refused. Code
  that caught `RpcError` to outlast an exit no longer catches it. Example: if its read fails
  with "client exited", `GameApi.local_health()` now raises instead of returning `None`.
- **Detaching from a client is safe against threads still using it.** The native host keeps
  its pointer after a detach frees it, so a host call made after the detach would use freed
  memory. Now:
  - After `NativeHost.detach()`, every host call raises the new `HostDetached`, a
    `ConnectionError`, and never reaches the native host. `walk_cancel()` becomes a no-op.
  - `detach()` frees the handle only once host calls on other threads have ended. It
    cancels a walk in flight first, then waits up to 2 s. If calls are still running, it
    logs this and leaves the handle attached. It returns whether the handle was freed.
  - `Connection.close()` joins the event pump before it detaches. It leaves the handle
    attached (logged) while a script thread is still running after `ScriptRunner.stop()`'s
    wait, so the connection manager's sweep never frees a client under a live script. It
    returns whether the handle was freed.
  - `detach()` also frees the handle after a transport failure has marked the host dead.
    Before, that handle stayed open.

### Notes

- Exit codes follow Python's own rules. `sys.exit(True)` exits with status 1, as in plain
  Python, so in a GUI run it is a crash. `sys.exit()`, `sys.exit(None)` and `sys.exit(0)`
  are clean ends: from `on_loop` or a self-running script, the run stops normally.
- An `on_loop` delay of `float("inf")` sleeps until the script is stopped. A negative
  number, NaN, a bool or a non-number ends the loop.
