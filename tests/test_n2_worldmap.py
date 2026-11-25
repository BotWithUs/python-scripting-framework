"""World map elements (botwithus.api.worldmap) over a fake ``_native.worldmap_elements`` in the
real binding's shape (native ABI 7).

Failure means: an area other than 28 read by default (the Prifddinas ghost would then sit at its
real tile), area_id dropped, a name filter that ignores the tooltip, a radius filter that is not
Chebyshev or not sorted nearest first, or a host error swallowed.
"""
import pytest

from botwithus.api import worldmap as W
from botwithus.api.definitions import CacheUnavailable
from botwithus.core import native as native_mod
from botwithus.core.native import NativeUnavailable

from fakes import FakeNative


def _e(area, element, x, y, plane=0, flags=0, category=2613, name="", tooltip=""):
    return {"area_id": area, "element_id": element, "x": x, "y": y, "plane": plane,
            "flags": flags, "category": category, "sprite_id": -1, "name": name,
            "tooltip": tooltip, "text_truncated": 0}


AREAS = {
    28: [_e(28, 3612, 3233, 3222), _e(28, 3640, 4576, 2665, flags=1),
         _e(28, 9, 3240, 3230, category=1157, name="Bob's Axes", tooltip="Shop"),
         _e(28, 10, 3300, 3300, plane=1, category=1157, tooltip="Lumbridge castle")],
    231: [_e(231, 3640, 2208, 3361)],
}


class MapNative(FakeNative):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.areas_read: list[int] = []

    def worldmap_elements(self, area_id=28):
        self.areas_read.append(area_id)
        if area_id == -1:
            return AREAS[28] + AREAS[231]
        if area_id not in AREAS:
            raise RuntimeError(f"world map area {area_id} not found")
        return AREAS[area_id]


def test_constants():
    assert (W.MAIN_AREA_ID, W.ALL_AREAS) == (28, -1)


def test_the_main_area_is_the_default_and_area_id_is_kept():
    be = MapNative()
    native_mod.set_backend(be)
    main = W.elements()
    assert be.areas_read == [28]
    assert [(e.area_id, e.element_id, e.x, e.y) for e in main][:2] == [(28, 3612, 3233, 3222), (28, 3640, 4576, 2665)]
    prif = W.elements(231)
    assert [(e.area_id, e.x, e.y) for e in prif] == [(231, 2208, 3361)]
    assert [e.area_id for e in W.elements(W.ALL_AREAS)] == [28, 28, 28, 28, 231]


def test_filters():
    native_mod.set_backend(MapNative())
    assert [e.element_id for e in W.elements(name="lumbridge")] == [10]   # matched on the tooltip
    assert [e.element_id for e in W.elements(name="AXES")] == [9]
    assert [e.element_id for e in W.elements(category=2613)] == [3612, 3640]
    assert [e.element_id for e in W.elements(plane=1)] == [10]
    near = W.elements(near=(3235, 3225), radius=10)
    assert [e.element_id for e in near] == [3612, 9]   # sorted nearest first; 10 is 75 away
    assert W.nearest(3235, 3225, category=2613).element_id == 3612
    assert W.nearest(0, 0, radius=5) is None


def test_errors():
    native_mod.set_backend(MapNative())
    with pytest.raises(CacheUnavailable, match="area 99 not found"):
        W.elements(99)
    native_mod.set_backend(FakeNative())
    with pytest.raises(NativeUnavailable, match="ABI 7"):
        W.elements()
