"""The package must refuse a native host or a frame of another protocol version.

Without these checks a v19 host feeding this v20 package decodes inventories, the producer
block, ground items and projectiles from the wrong offsets and raises nothing.
"""
import pytest

from botwithus.core import layout as L
from botwithus.core import native as native_mod
from botwithus.core.native import NativeHost, ProtocolMismatch
from botwithus.core.snapshot import SnapshotView

from fakes import FakeNative, build_snapshot

# The last version this package decoded before v20; any other number would do.
_PREVIOUS_PROTOCOL = L.PROTOCOL_VERSION - 1
# v19's frame size: the length a v19 host hands over.
_V19_SNAPSHOT_SIZE = 365_744


def test_attach_refuses_a_host_of_another_protocol_version():
    native_mod.set_backend(FakeNative(protocol_version=_PREVIOUS_PROTOCOL))
    with pytest.raises(ProtocolMismatch):
        NativeHost.attach(4242)


def test_attach_accepts_a_host_of_this_protocol_version():
    native_mod.set_backend(FakeNative())
    assert NativeHost.attach(4242).pid == 4242


def test_snapshot_view_refuses_a_frame_of_another_size():
    with pytest.raises(ValueError):
        SnapshotView(bytes(_V19_SNAPSHOT_SIZE))


def test_snapshot_view_accepts_a_frame_of_this_size():
    assert SnapshotView(build_snapshot()).game_state == L.STATE_IN_GAME
