"""Live plugin-UI smoke driver for this framework: runs tests/live/ui_smoke.py in bwu_host against a
client in the world, and decides pass or fail. Developer tooling; pytest does not collect it.
Adapted from the native host's own live plugin-UI driver: the same rpc.tap
connection and agentctl raw lookups, with checks for per-script release on a shared pipe.

usage: python ui_smoke_run.py --host <bwu_host.exe> --agentctl <agentctl.exe> --pid <client pid>
                              --src <this repo>/src [--mode cleanup|event|p2] [--python-root <dir>]
                              [--break-release]

cleanup: scripts A and B share one Connection (one agent pipe). While both run, raw lookups over
  agentctl find all six components. After A stops: A's four are gone and B's two are still found,
  and the tap shows one ui.destroy per A component, from the script's own pipe, answered freed,
  and no ui.destroy_all yet. A ui.destroy_all at that point would have taken B's too. After B
  stops: B's are gone the same way. The connection's close then sends the host's one
  ui.destroy_all, which must find none of the scripts' components left: its `destroyed` is
  exactly 0. (SmokeA and SmokeB both look up 1477:61. Observed agent behaviour: the agent
  dedups get_component per connection, so both get ONE Jagex handle, and destroy_all's count
  excludes Jagex lookups.)
  --break-release disables the framework's release; the run must then FAIL.
event: the script waits for a UiEvent while the driver takes the client to the lobby
  (exit_to_lobby), then logs it back in (login_to_game).
p2 (NXTLibrary a17edc4 or later, bwu_host from native-scripting-host 7bb38a4 or later;
  tests/live/ui_smoke_p2.py): scripts P2A and P2B share one Connection. P2A builds a panel with a
  button, a check, a slider, a list and an input_field, sets and reads back values, subscribes
  onselectionchanged (slider, list) and oncontentchanged (input) with the typed helpers, and sets
  values again through the script API: the content set must arrive as ONE typed UiContentChanged,
  and the slider and list sets must raise nothing (PUBLIC_PROTOCOL 6.12). P2B keeps a button with
  an onbuttonpressed subscription. On go_a P2A stops with no unsubscribe or destroy of its own; the
  rpc.tap must show A's three ui.subscribe answered, NO ui.unsubscribe, one ui.destroy per A
  component (freed) and no ui.destroy_all; raw lookups then find A's panel gone and B's button
  still there. After B stops and the connection closes, the backstop ui.destroy_all destroys 0 and
  B's button is gone. --break-release must FAIL.

Exit code 0 = every check passed. One "PASS|FAIL <check>" line per check.
"""
import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time

import msgpack

HERE = os.path.dirname(os.path.abspath(__file__))
IFACE, COMP = 1477, 61          # TOPLEVEL_V2__GAME_AREA
A_PARTS = ("a_layer", "a_text", "a_canvas", "a_inner")
B_PARTS = ("b_layer", "b_text")
# What the host's backstop ui.destroy_all may still destroy: nothing. Its `destroyed` counts
# agent components only, never the (deduped) Jagex lookup, so any count above 0 is a script
# component the framework failed to release.
BACKSTOP_DESTROYED = 0
READY_TIMEOUT_S = 60
EXIT_TIMEOUT_S = 60
TAP_SETTLE_S = 0.5
GAME_STATE_WORLD, GAME_STATE_LOBBY = 30, 20

failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f" ({detail})" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


class Tap:
    """A connection subscribed to rpc.tap, recording every ui.* call with its reply and time."""

    def __init__(self, pid):
        self.pipe = open(r"\\.\pipe\BotWithUs_%d" % pid, "r+b", buffering=0)
        self.calls = []
        self.lock = threading.Lock()
        body = msgpack.packb({"id": 1, "method": "_debug.subscribe", "params": {"topics": ["rpc.tap"]}})
        self.pipe.write(struct.pack("<I", len(body)) + body)
        threading.Thread(target=self._read, daemon=True).start()

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _read(self):
        try:
            while True:
                (length,) = struct.unpack("<I", self._exact(4))
                msg = msgpack.unpackb(self._exact(length), raw=False, strict_map_key=False)
                data = msg.get("data") if msg.get("topic") == "rpc.tap" else None
                if data and str(data.get("method", "")).startswith("ui."):
                    with self.lock:
                        self.calls.append((time.monotonic(), data["method"], data.get("params"), data.get("reply")))
        except (EOFError, OSError):
            pass

    def find(self, method, since, until=None):
        with self.lock:
            return [c for c in self.calls if c[1] == method and c[0] >= since
                    and (until is None or c[0] < until)]


def agentctl(args, method, params):
    out = subprocess.run([args.agentctl, "call", method, json.dumps(params), "--pid", str(args.pid)],
                         capture_output=True, text=True, timeout=30)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"ok": False, "error": out.stdout + out.stderr}


def lookup(args, key):
    return agentctl(args, "ui.get_component", {"iface": IFACE, "comp": COMP, "key": key})


def found(reply):
    return reply.get("ok") is True


def gone(reply):
    return "component not found" in json.dumps(reply)


def read_out(path):
    try:
        with open(path, encoding="utf-8") as f:
            return dict(line.strip().split("=", 1) for line in f if "=" in line)
    except OSError:
        return {}


def start_host(args, out_path, mode, script="ui_smoke.py"):
    env = dict(os.environ, BWU_SMOKE_PID=str(args.pid), BWU_SMOKE_OUT=out_path, BWU_SMOKE_MODE=mode,
               PYTHONPATH=args.src)
    if args.break_release:
        env["BWU_SMOKE_BREAK"] = "release"
    if args.python_root:
        env["PATH"] = args.python_root + os.pathsep + env.get("PATH", "")
    return subprocess.Popen([args.host, "--py", os.path.join(HERE, script)], env=env)


def wait_key(out_path, key, timeout_s=READY_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        values = read_out(out_path)
        if values.get(key):
            return values
        time.sleep(0.2)
    return None


def destroys_for(tap, handles, since, until):
    """The tap's ui.destroy calls for `handles` in the window, as {handle: freed}."""
    out = {}
    for _, _, params, reply in tap.find("ui.destroy", since, until):
        h = (params or {}).get("handle")
        if h in handles:
            out[h] = ((reply or {}).get("result") or {}).get("freed")
    return out


def check_lookups(args, values, parts, want, when):
    for name in parts:
        reply = lookup(args, int(values[f"{name}_key"]))
        ok = found(reply) if want == "found" else gone(reply)
        check(f"raw lookup: {name} {want} {when}", ok, json.dumps(reply)[:140])


def run_cleanup(args, tap, out_path):
    start = time.monotonic()
    host = start_host(args, out_path, "cleanup")
    values = wait_key(out_path, "ready")
    check("both scripts ready", values is not None)
    if values is None:
        host.kill()
        return
    handles = {name: int(values[f"{name}_handle"]) for name in A_PARTS + B_PARTS}
    check_lookups(args, values, A_PARTS + B_PARTS, "found", "while both run")
    check("one Jagex handle for both scripts' lookups (the agent dedups per connection)",
          values.get("a_root") is not None and values.get("a_root") == values.get("b_root"),
          f"A {values.get('a_root')} B {values.get('b_root')}")

    t_go_a = time.monotonic()
    open(out_path + ".go_a", "w").close()
    values = wait_key(out_path, "a_stopped")
    check("script A stopped", values is not None)
    time.sleep(TAP_SETTLE_S)
    t_a = time.monotonic()
    check_lookups(args, values, A_PARTS, "gone", "after A stopped")
    check_lookups(args, values, B_PARTS, "found", "after A stopped (B untouched)")
    a_handles = {handles[n] for n in A_PARTS}
    a_destroys = destroys_for(tap, a_handles, t_go_a, t_a)
    check("A's release: one ui.destroy per A component", set(a_destroys) == a_handles,
          f"destroyed {sorted(a_destroys)} want {sorted(a_handles)}")
    check("A's release: every one freed", all(v is True for v in a_destroys.values()), str(a_destroys))
    b_hit = destroys_for(tap, {handles[n] for n in B_PARTS}, t_go_a, t_a)
    check("A's release sent nothing for B", not b_hit, str(b_hit))
    check("no ui.destroy_all while B runs", not tap.find("ui.destroy_all", start, t_a))

    open(out_path + ".go_b", "w").close()
    code = host.wait(timeout=EXIT_TIMEOUT_S)
    check("bwu_host exit 0", code == 0, f"exit {code}")
    time.sleep(TAP_SETTLE_S)
    values = read_out(out_path)
    check("script reported done", values.get("done") == "1", str(values.get("done")))
    check_lookups(args, values, B_PARTS, "gone", "after the run")
    b_handles = {handles[n] for n in B_PARTS}
    b_destroys = destroys_for(tap, b_handles, t_a, None)
    check("B's release: one ui.destroy per B component, freed",
          set(b_destroys) == b_handles and all(v is True for v in b_destroys.values()), str(b_destroys))
    finals = [((r or {}).get("result") or {}).get("destroyed") for _, _, _, r in tap.find("ui.destroy_all", start)]
    check("the host's backstop ui.destroy_all found no script component left",
          finals == [BACKSTOP_DESTROYED], f"destroy_all destroyed {finals}")


P2_A_PARTS = ("panel", "button", "check", "slider", "list", "input")
P2_SUBSCRIBES = 3   # slider + list onselectionchanged, input oncontentchanged


def lookup_key(args, key):
    reply = lookup(args, int(key))
    return reply, found(reply), gone(reply)


def check_p2_script(values):
    check("typed UiContentChanged for the script's own content set",
          (values.get("content_type"), values.get("content_handle_ok"), values.get("content_reason"),
           values.get("content_reason_code"), values.get("content_text"))
          == ("UiContentChanged", "1", "content_change", "0", "bwu p2 smoke"),
          f"{values.get('content_type')} {values.get('content_reason')}/"
          f"{values.get('content_reason_code')}/{values.get('content_text')!r}")
    check("the same event reached ctx.events", values.get("content_in_ctx_events") == "1")
    check("slider and list sets raised no onselectionchanged", values.get("selection_events") == "0",
          str(values.get("selection_events")))
    threads = values.get("event_threads", "")
    check("events on the connection's own ui-events thread", threads.startswith("ui-events-")
          and "," not in threads, threads)
    check("check label refused verbatim by the agent",
          values.get("check_label_refusal", "").startswith("ui prop not supported yet: label"),
          values.get("check_label_refusal"))
    check("blur of an unfocused field answered false", values.get("blurred") == "0")
    print(f"INFO input_field renders={values.get('input_renders')} font_id={values.get('input_font_id')} "
          f"sprite_id={values.get('input_sprite_id')} (reported as the agent reads it)", flush=True)


def check_p2_tap(tap, a_handles, start, t_go_a, t_a):
    subs = [(p or {}).get("handle") for _, _, p, r in tap.find("ui.subscribe", start, t_go_a)
            if ((r or {}).get("result") or {}).get("subscribed") is True]
    check("A's three ui.subscribe answered subscribed",
          len([h for h in subs if h in a_handles]) == P2_SUBSCRIBES, f"subscribed {subs}")
    check("no ui.unsubscribe at all (the release takes subscriptions with the components)",
          not tap.find("ui.unsubscribe", start), str(tap.find("ui.unsubscribe", start)))
    destroys = destroys_for(tap, a_handles, t_go_a, t_a)
    check("A's release: one ui.destroy per A component", set(destroys) == a_handles,
          f"destroyed {sorted(destroys)} want {sorted(a_handles)}")
    check("A's release: every one freed", all(v is True for v in destroys.values()), str(destroys))
    check("no ui.destroy_all while B runs", not tap.find("ui.destroy_all", start, t_a))


def run_p2(args, tap, out_path):
    start = time.monotonic()
    host = start_host(args, out_path, "p2", "ui_smoke_p2.py")
    values = wait_key(out_path, "ready")
    check("both scripts ready", values is not None, str(read_out(out_path))[-300:])
    if values is None:
        host.kill()
        return
    check_p2_script(values)
    a_handles = {int(values[f"a_{n}_handle"]) for n in P2_A_PARTS}
    _, ok, _ = lookup_key(args, values["a_panel_key"])
    check("raw lookup: A's panel found while both run", ok)
    _, ok, _ = lookup_key(args, values["b_button_key"])
    check("raw lookup: B's button found while both run", ok)

    t_go_a = time.monotonic()
    open(out_path + ".go_a", "w").close()
    values = wait_key(out_path, "a_stopped")
    check("script A stopped", values is not None)
    time.sleep(TAP_SETTLE_S)
    t_a = time.monotonic()
    reply, _, is_gone = lookup_key(args, values["a_panel_key"])
    check("raw lookup: A's panel gone after A stopped", is_gone, json.dumps(reply)[:140])
    reply, ok, _ = lookup_key(args, values["b_button_key"])
    check("raw lookup: B's button still found after A stopped", ok, json.dumps(reply)[:140])
    check_p2_tap(tap, a_handles, start, t_go_a, t_a)

    open(out_path + ".go_b", "w").close()
    code = host.wait(timeout=EXIT_TIMEOUT_S)
    check("bwu_host exit 0", code == 0, f"exit {code}")
    time.sleep(TAP_SETTLE_S)
    values = read_out(out_path)
    check("script reported done", values.get("done") == "1", str(values.get("done")))
    reply, _, is_gone = lookup_key(args, values["b_button_key"])
    check("raw lookup: B's button gone after the run", is_gone, json.dumps(reply)[:140])
    b_handle = int(values["b_button_handle"])
    b_destroys = destroys_for(tap, {b_handle}, t_a, None)
    check("B's release: one ui.destroy, freed", b_destroys == {b_handle: True}, str(b_destroys))
    check("still no ui.unsubscribe", not tap.find("ui.unsubscribe", start))
    finals = [((r or {}).get("result") or {}).get("destroyed") for _, _, _, r in tap.find("ui.destroy_all", start)]
    check("the host's backstop ui.destroy_all found no script component left",
          finals == [BACKSTOP_DESTROYED], f"destroy_all destroyed {finals}")


def game_state(args):
    out = subprocess.run([args.agentctl, "snapshot", "--pid", str(args.pid)], capture_output=True, text=True, timeout=30)
    try:
        return json.loads(out.stdout).get("snapshot", {}).get("gameState")
    except ValueError:
        return None


def wait_state(args, state, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if game_state(args) == state:
            return True
        time.sleep(1)
    return False


def run_event(args, out_path):
    host = start_host(args, out_path, "event")
    check("script ready", wait_key(out_path, "ready") is not None)
    check("exit_to_lobby", agentctl(args, "exit_to_lobby", {}).get("ok") is True)
    code = host.wait(timeout=EXIT_TIMEOUT_S + 60)
    values = read_out(out_path)
    check("bwu_host exit 0", code == 0, f"exit {code}")
    check("UiEvent for the script's own layer", values.get("event_handle_ok") == "1", str(values))
    check("UiEvent destroyed/parent_gone via ctx.events.subscribe",
          (values.get("event_hook"), values.get("event_reason")) == ("destroyed", "parent_gone"),
          f"{values.get('event_hook')}/{values.get('event_reason')}")
    check("the event took the layer off the script's list", values.get("layer_alive_after_event") == "0")
    check("a destroy after the event is UiRefused unknown handle", values.get("after_event") == "refused",
          str(values.get("after_event")))
    threads = values.get("event_threads", "")
    check("UiEvents arrive on the connection's own ui-events thread, not the ring pump",
          threads.startswith("ui-events-") and "," not in threads, threads)
    print(f"INFO events seen: {values.get('all_events')}; Jagex root {values.get('root_handle')} "
          f"alive after: {values.get('root_alive_after_event')}", flush=True)
    check("back in the lobby", wait_state(args, GAME_STATE_LOBBY, 30))
    agentctl(args, "login_to_game", {})
    check("logged back in", wait_state(args, GAME_STATE_WORLD, 120))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--agentctl", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--src", required=True, help="this repo's src dir (PYTHONPATH for bwu_host)")
    parser.add_argument("--mode", choices=("cleanup", "event", "p2"), default="cleanup")
    parser.add_argument("--python-root")
    parser.add_argument("--break-release", action="store_true")
    args = parser.parse_args()
    tap = Tap(args.pid)
    with tempfile.TemporaryDirectory() as scratch:
        out_path = os.path.join(scratch, "smoke.txt")
        if args.mode == "cleanup":
            run_cleanup(args, tap, out_path)
        elif args.mode == "p2":
            run_p2(args, tap, out_path)
        else:
            run_event(args, out_path)
    print(f"ui_smoke_run py-framework {args.mode}: {len(failures)} failure(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
