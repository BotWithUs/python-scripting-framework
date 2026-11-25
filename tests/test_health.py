"""GameApi.local_health: one batched varp read per server tick, and the unknowns.

The call-counting tests are the point. A health read that fires per call would
spend a round-trip every time a decision loop glances at HP; one that never
re-fires would pin the first reading forever.

Health is read through the native host's varp state reads (``read_varps``), so a
reading also carries a state. Only SET, or a DEFAULT with a verified default, is
a known reading; everything else is ``None`` and is never cached.
"""
import pytest

from botwithus.api.game import VARP_CURRENT_HEALTH, VARP_MAX_HEALTH, GameApi
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost, NativeUnavailable
from botwithus.core.rpc import RpcClient
from botwithus.core.snapshot import SnapshotView
from botwithus.core.variables import VariableReader

from fakes import FakeNative, build_snapshot, var_record

CURRENT_HP = 3210
MAX_HP = 9900
NO_VALUE = FakeNative.VAR_NO_VALUE
SET = FakeNative.VARP_SET
DEFAULT = FakeNative.VARP_DEFAULT_NOT_SET_CLIENTSIDE
UNAVAILABLE = FakeNative.VARP_UNAVAILABLE
NO_SUCH = FakeNative.VARP_NO_SUCH_VARP
INT = FakeNative.VAR_KIND_INT


def _health(current, maximum, state=SET, verified=True):
    """A read_varps reply for the health pair, both in ``state``."""
    return [var_record(VARP_CURRENT_HEALTH, state, current, kind=INT, verified=verified),
            var_record(VARP_MAX_HEALTH, state, maximum, kind=INT, verified=verified)]


class _Recorder:
    """Counts read_varps calls and serves scripted replies (the last one repeats)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[int]] = []

    def __call__(self, fn, ids):
        if fn != "read_varps":
            return []
        self.calls.append(list(ids))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _api(recorder, ticks, backend=None):
    """A GameApi whose snapshot advances through ``ticks`` on each read."""
    native_mod.set_backend(backend or FakeNative(var_reads=recorder))
    host = NativeHost.attach(4242)
    supply = iter(ticks)
    return GameApi(RpcClient(host),
                   lambda: SnapshotView(build_snapshot(server_tick=next(supply))),
                   VariableReader(host))


def test_reads_both_varps_in_one_batched_call():
    rec = _Recorder(_health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100])
    assert api.local_health() == (CURRENT_HP, MAX_HP)
    assert rec.calls == [[VARP_CURRENT_HEALTH, VARP_MAX_HEALTH]]


def test_ten_calls_in_one_tick_cost_exactly_one_read():
    rec = _Recorder(_health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100] * 10)
    results = [api.local_health() for _ in range(10)]
    assert results == [(CURRENT_HP, MAX_HP)] * 10
    assert len(rec.calls) == 1, "cached per server tick"


def test_the_next_tick_re_reads():
    rec = _Recorder(_health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100, 100, 101, 101])
    api.local_health()
    api.local_health()
    assert len(rec.calls) == 1
    api.local_health()
    api.local_health()
    assert len(rec.calls) == 2, "a new server tick invalidates the sample"


def test_rpc_failure_degrades_to_unknown():
    api = _api(lambda fn, ids: "__error__:varp read failed", [100])
    assert api.local_health() is None


def test_a_failed_read_is_not_cached():
    """Otherwise one hiccup pins 'unknown' for the rest of the tick."""
    rec = _Recorder("__error__:nope", _health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100, 100])
    assert api.local_health() is None
    assert api.local_health() == (CURRENT_HP, MAX_HP), "retried inside the same tick"


def test_unavailable_is_unknown_not_minus_one():
    """The host reports a failed per-id read as UNAVAILABLE / -1, not as an error."""
    api = _api(_Recorder(_health(NO_VALUE, NO_VALUE, state=UNAVAILABLE)), [100])
    assert api.local_health() is None


def test_unavailable_is_not_cached():
    rec = _Recorder(_health(NO_VALUE, NO_VALUE, state=UNAVAILABLE), _health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100, 100])
    assert api.local_health() is None
    assert api.local_health() == (CURRENT_HP, MAX_HP), "re-read inside the same tick"
    assert len(rec.calls) == 2


def test_one_unavailable_varp_makes_the_pair_unknown():
    reply = [var_record(VARP_CURRENT_HEALTH, SET, CURRENT_HP, kind=INT),
             var_record(VARP_MAX_HEALTH, UNAVAILABLE, NO_VALUE)]
    api = _api(_Recorder(reply), [100])
    assert api.local_health() is None


def test_no_such_varp_is_unknown():
    api = _api(_Recorder(_health(NO_VALUE, NO_VALUE, state=NO_SUCH)), [100])
    assert api.local_health() is None


def test_an_unverified_default_is_unknown():
    """Before the cache warms up the host can only guess the default (0)."""
    api = _api(_Recorder(_health(0, 0, state=DEFAULT, verified=False)), [100])
    assert api.local_health() is None


def test_an_unverified_default_is_not_cached():
    rec = _Recorder(_health(0, 0, state=DEFAULT, verified=False), _health(CURRENT_HP, MAX_HP))
    api = _api(rec, [100, 100])
    assert api.local_health() is None
    assert api.local_health() == (CURRENT_HP, MAX_HP), "re-read inside the same tick"
    assert len(rec.calls) == 2


def test_a_verified_default_is_a_real_reading():
    api = _api(_Recorder(_health(0, MAX_HP, state=DEFAULT)), [100])
    assert api.local_health() == (0, MAX_HP)


def test_short_result_is_unknown_not_a_shifted_value():
    """A truncated batch must not let max slide into current's slot."""
    reply = [var_record(VARP_CURRENT_HEALTH, SET, CURRENT_HP, kind=INT)]
    api = _api(_Recorder(reply), [100])
    assert api.local_health() is None


def test_reordered_result_is_unknown_not_a_swapped_value():
    api = _api(_Recorder(list(reversed(_health(CURRENT_HP, MAX_HP)))), [100])
    assert api.local_health() is None


def test_empty_result_is_unknown():
    api = _api(_Recorder([]), [100])
    assert api.local_health() is None


def test_zero_health_is_a_real_reading_not_an_unknown():
    """A dead player reads 0, and 0 is not None."""
    api = _api(_Recorder(_health(0, MAX_HP)), [100])
    assert api.local_health() == (0, MAX_HP)


def test_connection_error_is_not_swallowed():
    """A dead transport is fatal; reporting 'unknown health' would hide it."""
    api = _api(lambda fn, ids: "__error__:agent pipe gone", [100])
    with pytest.raises(ConnectionError):
        api.local_health()


def test_an_older_native_host_is_refused_not_unknown():
    """A bwu_host without varp state reads can never answer; say so, don't return None."""
    class Old(FakeNative):
        pass

    for name in ("read_varps", "VARP_SET"):
        setattr(Old, name, None)
    api = _api(None, [100], backend=Old())
    with pytest.raises(NativeUnavailable):
        api.local_health()
