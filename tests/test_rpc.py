"""RPC behaviour over the native host seam: result unwrap, error mapping, params, death.

Id correlation and broker-push skipping now live in the native host (``AgentPipe`` in
bwu_host), so they are not re-tested here; what this suite pins is the Python seam --
``NativeHost.rpc`` / ``RpcClient.call`` packing the request, unwrapping the result, and
mapping failures onto the right exception.
"""
import pytest

from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost
from botwithus.core.rpc import RpcClient, RpcError

from fakes import FakeNative


def _client(responder) -> RpcClient:
    be = FakeNative(responder=responder)
    native_mod.set_backend(be)
    return RpcClient(NativeHost.attach(4242))


def test_call_returns_result():
    c = _client(lambda method, params: {"pong": True} if method == "rpc.ping" else {})
    assert c.call("rpc.ping") == {"pong": True}


def test_error_envelope_raises_rpc_error():
    c = _client(lambda method, params: "__error__:method not found: nope")
    with pytest.raises(RpcError) as ei:
        c.call("nope")
    assert "method not found" in ei.value.agent_message
    assert ei.value.method == "nope"


def test_params_are_forwarded():
    seen = {}

    def responder(method, params):
        seen["method"], seen["params"] = method, params
        return {}

    c = _client(responder)
    c.call("get_login_state")
    assert seen["params"] == {}  # no-arg call sends an empty param map
    c.call("get_varp", {"var_id": 5})
    assert seen["params"] == {"var_id": 5}


def test_transport_death_marks_host_and_raises_connection_error():
    be = FakeNative(responder=lambda m, p: "__error__:pipe disconnected")
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    assert host.alive
    with pytest.raises(ConnectionError):
        host.rpc("anything")
    assert not host.alive  # a transport failure is terminal, not a per-call error
