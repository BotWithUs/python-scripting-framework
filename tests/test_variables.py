"""Varp / varbit reads over the native host's read_varps / read_varbits.

The native host does the composition (agent state + cache default) and is tested there; this
suite pins what the Python layer adds: decoding by the native module's own constants, the
GameApi conveniences, and the error mapping.
"""
import pytest

from botwithus.api.game import GameApi
from botwithus.api.variables import NO_VALUE, VarbitRead, VarKind, VarpRead, VarpState
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost, NativeUnavailable
from botwithus.core.rpc import RpcClient, RpcError
from botwithus.core.snapshot import SnapshotView
from botwithus.core.variables import VariableReader

from fakes import FakeNative, var_record

SET, DEFAULT, NO_SUCH, UNAVAILABLE = (FakeNative.VARP_SET, FakeNative.VARP_DEFAULT_NOT_SET_CLIENTSIDE,
                                      FakeNative.VARP_NO_SUCH_VARP, FakeNative.VARP_UNAVAILABLE)
INT, LONG = FakeNative.VAR_KIND_INT, FakeNative.VAR_KIND_LONG


def _game(var_reads, be=None):
    be = be or FakeNative(var_reads=var_reads)
    native_mod.set_backend(be)
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()), VariableReader(host))
    return game, be, host


def _canned(records_by_id):
    return lambda fn, ids: [records_by_id[i] for i in ids]


def test_every_state_decodes():
    game, _, _ = _game(_canned({
        13537: var_record(13537, SET, 2350, kind=INT),
        193: var_record(193, DEFAULT, -1),
        20000: var_record(20000, NO_SUCH, NO_VALUE),
        -5: var_record(-5, UNAVAILABLE, NO_VALUE),
        7: var_record(7, DEFAULT, 0, verified=False),
    }))
    reads = game.read_varps([13537, 193, 20000, -5, 7])
    assert reads == [
        VarpRead(13537, VarpState.SET, 2350, 2350, VarKind.INT, True),
        VarpRead(193, VarpState.DEFAULT_NOT_SET_CLIENTSIDE, -1, -1, VarKind.UNKNOWN, True),
        VarpRead(20000, VarpState.NO_SUCH_VARP, -1, -1, VarKind.UNKNOWN, True),
        VarpRead(-5, VarpState.UNAVAILABLE, -1, -1, VarKind.UNKNOWN, True),
        VarpRead(7, VarpState.DEFAULT_NOT_SET_CLIENTSIDE, 0, 0, VarKind.UNKNOWN, False),
    ]
    assert [r.is_set for r in reads] == [True, False, False, False, False]
    assert [r.has_value for r in reads] == [True, True, False, False, True]


def test_a_set_minus_one_is_still_set():
    game, _, _ = _game(_canned({4: var_record(4, SET, -1, kind=INT)}))
    read = game.read_varp(4)
    assert read.is_set and read.value == -1
    assert game.varp_state(4) is VarpState.SET


def test_long_varp_keeps_all_64_bits():
    whole = 0x123456789ABCDEF0
    low = whole & 0xFFFFFFFF
    low_signed = low - (1 << 32) if low >= 1 << 31 else low
    game, _, _ = _game(_canned({12921: var_record(12921, SET, low_signed, value64=whole, kind=LONG)}))
    read = game.read_varp(12921)
    assert read.kind is VarKind.LONG
    assert game.varp_long(12921) == whole
    assert game.varp(12921) == low_signed


def test_varp_and_varps_return_what_the_game_reads():
    game, _, _ = _game(_canned({
        1: var_record(1, SET, 5), 2: var_record(2, DEFAULT, -1), 3: var_record(3, UNAVAILABLE, -1),
    }))
    assert game.varp(2) == -1          # the cache default, not the agent's 0
    assert game.varps([1, 2, 3]) == [5, -1, -1]


def test_varbits_decode():
    game, be, _ = _game(_canned({
        10: var_record(10, DEFAULT, 15), 11: var_record(11, SET, 3), 12: var_record(12, NO_SUCH, -1),
    }))
    assert game.read_varbits([10, 11, 12]) == [
        VarbitRead(10, VarpState.DEFAULT_NOT_SET_CLIENTSIDE, 15, True),
        VarbitRead(11, VarpState.SET, 3, True),
        VarbitRead(12, VarpState.NO_SUCH_VARP, -1, True),
    ]
    assert game.varbit(11) == 3
    assert be.var_calls[0] == ("read_varbits", [10, 11, 12])


def test_states_map_through_the_native_constants_not_by_number():
    class Renumbered(FakeNative):
        VARP_SET = 7
        VARP_DEFAULT_NOT_SET_CLIENTSIDE = 8
        VARP_NO_SUCH_VARP = 9
        VARP_UNAVAILABLE = 10
        VAR_KIND_INT = 20
        VAR_KIND_LONG = 21
        VAR_KIND_STRING = 22
        VAR_KIND_UNKNOWN = 23

    be = Renumbered(var_reads=lambda fn, ids: [var_record(1, 7, 4, kind=21), var_record(2, 8, 0),
                                               var_record(3, 2, 0)])
    game, _, _ = _game(None, be)
    a, b, c = game.read_varps([1, 2, 3])
    assert (a.state, a.kind) == (VarpState.SET, VarKind.LONG)
    assert b.state is VarpState.DEFAULT_NOT_SET_CLIENTSIDE
    assert c.state is VarpState.UNAVAILABLE   # 2 is not a state in this numbering


def test_an_out_of_contract_number_reads_unavailable_and_unknown():
    game, _, _ = _game(lambda fn, ids: [var_record(1, 99, 4, kind=42)])
    read = game.read_varp(1)
    assert (read.state, read.kind) == (VarpState.UNAVAILABLE, VarKind.UNKNOWN)


def test_any_iterable_of_ids_is_passed_as_a_list():
    game, be, _ = _game(lambda fn, ids: [var_record(i, SET, i) for i in ids])
    assert [r.value for r in game.read_varps(range(600))] == list(range(600))
    assert be.var_calls == [("read_varps", list(range(600)))]   # one call: the host chunks


def test_a_failed_read_is_an_rpc_error():
    game, _, host = _game(lambda fn, ids: "__error__:rpc timeout")
    with pytest.raises(RpcError):
        game.read_varp(1)
    assert host.alive


def test_a_dead_transport_marks_the_host_dead():
    game, _, host = _game(lambda fn, ids: "__error__:pipe disconnected")
    with pytest.raises(ConnectionError):
        game.read_varps([1])
    assert not host.alive


def test_an_older_native_host_is_refused_loudly():
    class Old(FakeNative):
        pass

    for name in ("read_varps", "read_varbits", "varp_defaults_status", "VARP_SET"):
        setattr(Old, name, None)
    game, _, _ = _game(None, Old())
    with pytest.raises(NativeUnavailable):
        game.read_varp(1)


def test_defaults_status_is_forwarded():
    _, _, host = _game(None, FakeNative(defaults_status=-1))
    assert VariableReader(host).defaults_status() == -1


def test_a_game_api_without_a_reader_says_so():
    native_mod.set_backend(FakeNative())
    host = NativeHost.attach(4242)
    game = GameApi(RpcClient(host), lambda: SnapshotView(host.snapshot_bytes()))
    with pytest.raises(RuntimeError, match="variable reader"):
        game.varp(1)
