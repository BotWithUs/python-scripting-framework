"""A maintainer's REPL: attach to every running client, list/start/stop scripts.

It needs ``botwithus._native``, which exists only inside the BotWithUs native host
(``bwu_host``), so it cannot run under a plain Python; run scripts with ``bwu_host --py
<script>`` instead. Outside the host it exits at once with a one-line message.

Commands in the REPL:
    clients                 list attached game clients
    scripts                 list discoverable scripts
    start <name> [pid]      start a script (defaults to the only/first client)
    stop <name> [pid]       stop a running script
    running                 list running scripts per client
    quit                    stop everything and exit
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..core import native
from ..core.loader import ScriptLoader
from .manager import ConnectionManager


def _print_clients(mgr: ConnectionManager) -> None:
    conns = mgr.connections()
    if not conns:
        print("no clients attached")
        return
    for conn in conns:
        snap = conn.snapshot()
        print(f"  pid {conn.pid}  state={snap.game_state}  serverTick={snap.server_tick}  "
              f"running={conn.running_scripts()}")


def _print_scripts(loader: ScriptLoader) -> None:
    scripts = loader.discover()
    if not scripts:
        print("no scripts discovered (drop .py files in ~/.botwithus/scripts/python/)")
        return
    for s in scripts:
        m = s.manifest
        print(f"  {m.name}  v{m.version}  by {m.author}  [{s.origin}]")


def _resolve_pid(mgr: ConnectionManager, arg: str | None) -> int | None:
    conns = mgr.connections()
    if arg is not None:
        return int(arg)
    if len(conns) == 1:
        return conns[0].pid
    return conns[0].pid if conns else None


def _repl(mgr: ConnectionManager, loader: ScriptLoader) -> None:
    print("botwithus host -- type 'quit' to exit, 'clients'/'scripts' to inspect")
    while True:
        try:
            line = input("bwu> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        if cmd == "quit":
            break
        elif cmd == "clients":
            _print_clients(mgr)
        elif cmd == "scripts":
            _print_scripts(loader)
        elif cmd == "running":
            _print_clients(mgr)
        elif cmd in ("start", "stop"):
            if not args:
                print(f"usage: {cmd} <name> [pid]")
                continue
            name = args[0]
            pid = _resolve_pid(mgr, args[1] if len(args) > 1 else None)
            conn = mgr.connection(pid) if pid is not None else None
            if conn is None:
                print("no matching client")
                continue
            if cmd == "start":
                loaded = loader.by_name(name)
                if loaded is None:
                    print(f"no script named {name!r}")
                    continue
                try:
                    conn.start_script(loaded.create())
                    print(f"started {name} on pid {conn.pid}")
                except Exception as exc:
                    print(f"start failed: {exc}")
            else:
                conn.stop_script(name)
                print(f"stopped {name} on pid {conn.pid}")
        else:
            print(f"unknown command: {cmd}")


NOT_IN_HOST = ("botwithus needs the BotWithUs native host: run scripts through bwu_host "
               "(bwu_host --py <script>).")


def main() -> int:
    parser = argparse.ArgumentParser(prog="botwithus.host")
    parser.add_argument("--list", action="store_true", help="print clients + scripts and exit")
    parser.add_argument("--no-nav", action="store_true", help="disable WorldWalker navigation")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ns = parser.parse_args()
    try:
        native.backend()
    except native.NativeUnavailable:
        print(NOT_IN_HOST, file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mgr = ConnectionManager(enable_navigation=not ns.no_nav)
    loader = ScriptLoader()
    mgr.start()
    try:
        if ns.list:
            print("clients:")
            _print_clients(mgr)
            print("scripts:")
            _print_scripts(loader)
            return 0
        _repl(mgr, loader)
        return 0
    finally:
        mgr.close()


if __name__ == "__main__":
    sys.exit(main())
