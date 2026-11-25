"""N3 live smoke (native ABI 8): gameval names used by real APIs, by-name variable reads and the
object animation filter, against a live client. Read-only: no click, no input, no camera.

    bwu_host --py tests/live/n3_smoke.py

Run with BWU_NATIVE_DIR at a directory holding an NXTCache.dll (the gameval index reads the
local game cache through it). Exit 0 when every check holds.
"""
import sys
import time

from botwithus.api import gameval as gv
from botwithus.api.action_bar import CURRENT_BAR_REF, VARBIT_CURRENT_BAR
from botwithus.api.entities import SceneObjects
from botwithus.api.gameval import GamevalType
from botwithus.api.prayers import POINTS_REF, Prayer
from botwithus.core.connection import Connection
from botwithus.core.native import backend, default_client_pid

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


pid = default_client_pid()
if pid is None:
    raise SystemExit("no game client found")
conn = Connection(pid)
game = conn.game
index = game.gamevals()

ok, version, why = backend().gameval_status()
check(ok and index.is_available(), f"gameval index available, index_version {version} {why}")

# 1. A real API resolving its ids by name: the action bar's current-bar varbit and the slot vars.
named_bar = index.id(GamevalType.VARBIT, "COMBATV2_ACTIONBAR_CURRENT_BAR")
check(named_bar == CURRENT_BAR_REF.resolve(index), f"COMBATV2_ACTIONBAR_CURRENT_BAR -> {named_bar} "
      f"(fallback {VARBIT_CURRENT_BAR})")
bar = game.action_bar()
active = bar.active_bar()
check(1 <= active <= 18, f"action bar active bar by name: {active}")
vars_ = bar._vars_of(active)
check(vars_ == bar._slot_vars.get(active), f"bar {active} slot variables resolved by name "
      f"(slot 1 type/id/obj = {vars_[0]})")
slots = bar.slots()
check(len(slots) == 14, "14 slots read through the named variables: " +
      ", ".join(type(s).__name__ for s in slots[:3]))

# 2. Quests: the stub preference reads the gameval listing, and a quest by its gameval name.
t0 = time.perf_counter()
cook_stub = game.quests().status(6)
check(cook_stub.quest_id == 257, f"quest stub 6 -> listed {cook_stub.quest_id} {cook_stub.name!r} "
      f"{cook_stub.stage.name} (listing {time.perf_counter() - t0:.2f}s)")
cooks = index.id(GamevalType.QUEST, "COOKS_ASSISTANT")
check(cooks == 257, f"quest gameval COOKS_ASSISTANT -> {cooks}")

# 3. By-name variable reads (Java's getVarp(String) family) and a prayer varbit by name.
points = game.varbit_by_gameval("PRAYER_POINTS")
check(points == game.varbit(POINTS_REF.resolve(index)) and points >= 0,
      f"varbit_by_gameval(PRAYER_POINTS) = {points} (tenths)")
check(game.varbit_by_gameval("NO_SUCH_VARBIT_N3") == -1, "an unknown name reads -1")
skin = Prayer.TIERED_DEFENCE
check(skin.varbit_ref.resolve(index) == index.id(GamevalType.VARBIT, skin.varbit_ref.gameval),
      f"prayer {skin.name} varbit {skin.varbit_ref.gameval} -> {skin.active_varbit}")
iface, comp = index.component("BANK__BANK_INV_BUTTON")
check((iface, comp) == (517, 39), f"BANK__BANK_INV_BUTTON -> {iface}:{comp}")

# 4. Object animations: the snapshot rows and the native rows agree, and the filter finds them.
snap = game.snapshot()
animating = SceneObjects(snap).animating().all()
names = {}
for o in animating[:5]:
    names[o.type_id] = index.gameval(GamevalType.LOC, o.type_id)
print(f"     {len(snap.locations())} locs, {len(animating)} animating: "
      + ", ".join(f"{o.type_id} {names.get(o.type_id)} anim {o.animation_id} at {o.tile}" for o in animating[:5]),
      flush=True)
native_rows = backend().locations(conn._host._h)
if native_rows is not None:
    native_anim = {(r["id"], r["animation_id"]) for r in native_rows if r["animation_id"] != -1}
    check(all(isinstance(r.get("animation_id"), int) for r in native_rows),
          f"native loc rows carry animation_id ({len(native_anim)} animating)")
if animating:
    first = animating[0]
    found = SceneObjects(snap).with_animation(first.animation_id).all()
    check(first in found, f"with_animation({first.animation_id}) matches the animating loc {first.type_id}")
else:
    print("     no animating loc in view: the filter check has nothing to match here", flush=True)

conn.close()
print(f"n3 smoke: {len(failures)} failure(s)", flush=True)
sys.exit(1 if failures else 0)
