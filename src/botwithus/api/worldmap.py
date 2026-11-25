"""World map elements: lodestones, place labels, quest starts, fairy rings and the like.

Read from the game cache (index 41) through the native host's NXTCache link
(``_native.worldmap_elements``, native ABI 7), each placement joined with its definition.
Most minimap function icons (banks, altars, trees) are not here: they are locs.

**One area at a time, the main world by default.** Every placement belongs to a world map area,
and its tile is in that area's map space. For nearly every placement that is the world tile. The
exception is the main overview area (:data:`MAIN_AREA_ID`, 28), which draws a few regions
(Prifddinas, Ashdale, ...) away from their real tiles and places them again, at the real tile,
in their own area: the Prifddinas lodestone is (4576, 2665) in area 28 and (2208, 3361) in area
231. So :func:`elements` reads area 28 unless told otherwise, as the Java host's
``WorldMapElements`` does; :data:`ALL_AREAS` reads every area, and those regions then appear
twice. Keep ``area_id`` when you need world tiles.

Many definitions carry no name (the lodestones among them): match those by ``category`` and
tile, or read the definition's JSON with ``Definitions.json(CACHE_WORLDMAP, element_id)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core import native
from .definitions import CacheUnavailable

MAIN_AREA_ID = 28       # the overview of the whole surface world (BWU_WORLDMAP_MAIN_AREA)
ALL_AREAS = -1          # every area (BWU_WORLDMAP_ALL_AREAS)


@dataclass(frozen=True, slots=True)
class MapElement:
    area_id: int
    element_id: int          # the definition id
    x: int                   # tile in the area's map space
    y: int
    plane: int
    flags: int               # the placement's raw trailing byte (meaning not pinned)
    category: int            # the definition's category (WME opcode 19); -1 unset
    sprite_id: int           # -1 unset
    name: str
    tooltip: str
    text_truncated: int

    def distance_to(self, x: int, y: int) -> int:
        """Chebyshev distance in tiles."""
        return max(abs(self.x - x), abs(self.y - y))

    @classmethod
    def from_native(cls, d: dict[str, Any]) -> "MapElement":
        return cls(int(d["area_id"]), int(d["element_id"]), int(d["x"]), int(d["y"]),
                   int(d["plane"]), int(d["flags"]), int(d["category"]), int(d["sprite_id"]),
                   str(d["name"]), str(d["tooltip"]), int(d["text_truncated"]))


def _read(area_id: int) -> list[MapElement]:
    fn = getattr(native.backend(), "worldmap_elements", None)
    if fn is None:
        raise native.NativeUnavailable("this bwu_host has no worldmap_elements; it predates them"
                                       " (native ABI 7) -- update it")
    try:
        raw = fn(int(area_id))
    except RuntimeError as exc:
        raise CacheUnavailable(str(exc)) from exc
    return [MapElement.from_native(d) for d in raw]


def elements(area_id: int = MAIN_AREA_ID, *, name: str | None = None, category: int | None = None,
             near: tuple[int, int] | None = None, radius: int | None = None,
             plane: int | None = None) -> list[MapElement]:
    """The placements of one area (the main world by default), filtered.

    ``name`` matches a case-insensitive substring of the name or the tooltip, as the Java host
    does; ``category`` matches exactly; ``near`` + ``radius`` keep placements within ``radius``
    tiles (Chebyshev) of ``near``, sorted nearest first; ``plane`` matches exactly. Raises
    :class:`CacheUnavailable` for an area the cache does not have."""
    found = _read(area_id)
    if name is not None:
        needle = name.casefold()
        found = [e for e in found if needle in e.name.casefold() or needle in e.tooltip.casefold()]
    if category is not None:
        found = [e for e in found if e.category == category]
    if plane is not None:
        found = [e for e in found if e.plane == plane]
    if near is not None:
        cx, cy = near
        if radius is not None:
            found = [e for e in found if e.distance_to(cx, cy) <= radius]
        found.sort(key=lambda e: e.distance_to(cx, cy))
    return found


def nearest(x: int, y: int, *, area_id: int = MAIN_AREA_ID, name: str | None = None,
            category: int | None = None, radius: int | None = None) -> MapElement | None:
    """The placement nearest (x, y) that matches, or None."""
    found = elements(area_id, name=name, category=category, near=(x, y), radius=radius)
    return found[0] if found else None
