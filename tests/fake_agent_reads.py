"""A recording fake agent for the RPC-backed reads and clicks (debug draw, component ops,
obj vars, spot anims, action history, inventory and dialog clicks).

Replies are written in the wire's own keys, copied from the agent's handlers (NXTLibrary
``src/rpc/Handlers.cpp`` at main b855e39), so a wrapper that reads a key the agent never
writes fails here. ``replies`` maps a method to its result, or to a callable
``(params) -> result``; a result string ``"__error__:<msg>"`` makes the call fail the way the
native host reports an agent error. A method with no reply fails as an unknown method would.
"""
from __future__ import annotations

from typing import Any, Callable

from botwithus.api.game import GameApi
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient

from fakes import FakeNative


class RecordingAgent:
    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies: dict[str, Any] = dict(replies or {})
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict | None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method in ("queue_action", "queue_actions") and method not in self.replies:
            return {} if method == "queue_action" else {"queued": len(params["actions"])}
        if method not in self.replies:
            return f"__error__:unknown method {method}"
        reply = self.replies[method]
        return reply(params or {}) if callable(reply) else reply

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def queued(self) -> list[tuple[int, int, int, int]]:
        """Every queue_action as (action_id, param1, param2, param3)."""
        return [(p["action_id"], p["param1"], p["param2"], p["param3"])
                for m, p in self.calls if m == "queue_action"]


def game_with(agent: RecordingAgent, snapshot: Callable[[], Any] | None = None) -> GameApi:
    native_mod.set_backend(FakeNative(responder=agent))
    return GameApi(RpcClient(NativeHost.attach(4242)), snapshot or (lambda: None))
