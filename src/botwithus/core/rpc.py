"""RPC over the native host surface.

The transport is gone from Python: the named pipe, the one-lock I/O model, id correlation,
and push-skipping all live in the native host now (``AgentPipe`` inside ``bwu_host``). What
remains here is the thin request/response *shape* the API layer binds to -- ``call(method,
params) -> result`` -- delegated to a :class:`~botwithus.core.native.NativeHost` (duck-typed
as anything with ``.rpc`` and ``.alive`` so this module needs no import from ``native`` and
stays cycle-free).

The pipe pub/sub broker (``_debug.subscribe`` / ``rpc.tap``) is intentionally NOT surfaced
here: script-facing events come from the shared-memory ring (see ``events``/``connection``),
and the native host's synchronous RPC drops broker pushes rather than delivering them
asynchronously. If a debug tap is ever needed it belongs on a dedicated native call, not on
this hot path.
"""
from __future__ import annotations

from typing import Any


class RpcError(RuntimeError):
    """The agent returned an ``{"id", "error"}`` envelope for a call."""

    def __init__(self, method: str, message: str) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.agent_message = message


class MalformedReply(RpcError):
    """The agent answered, but not in the shape the contract gives that method. An
    :class:`RpcError` so that one ``except`` covers every way a call can fail to give an answer."""


class RpcClient:
    """Request/response over a native host handle."""

    def __init__(self, host: Any) -> None:
        self._host = host

    @property
    def pid(self) -> int:
        return self._host.pid

    @property
    def closed(self) -> bool:
        return not self._host.alive

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make a call and return its ``result`` (raising :class:`RpcError` on error)."""
        return self._host.rpc(method, params)

    def close(self) -> None:
        # The host handle's lifetime is owned by the Connection; nothing to close here.
        pass
