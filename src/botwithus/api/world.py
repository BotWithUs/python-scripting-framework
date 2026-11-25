"""World positions and shapes: :class:`Coordinate` and the areas a script tests tiles against.

Mirrors the Java host's ``api.world`` package (JBotWithUsV2, phase J1): ``Coordinate``,
``Area``, ``RectangularArea``, ``CircularArea`` and ``PolygonalArea``.

**Not the walker's avoid area.** ``botwithus.api.Area`` (``core.walk.Area``) is the rectangle
the walker routes round. The shapes here are for testing where things are, and are exported at
the top level under their own names (:class:`RectangularArea`, ...). The base class is
``botwithus.api.world.Area``. :meth:`RectangularArea.to_avoid` turns a rectangle into the
walker's form.

Anything with ``x``/``y``/``plane`` (a :class:`Tile`, a :class:`Coordinate`) or with
``tile_x``/``tile_y``/``plane`` (an NPC, player, scene object or ground item row) can be tested.
"""
from __future__ import annotations

import math
import random as _random
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .snapshot_types import Tile

REGION_SIZE = 64
_REGION_SHIFT = 6
_REGION_ID_X_SHIFT = 8
_REGION_LOCAL_MASK = REGION_SIZE - 1
MAX_PLANE = 3
DEFAULT_REACHABLE_ATTEMPTS = 10
MIN_POLYGON_VERTICES = 3
_MAX_REJECTION_DRAWS = 64


def position_of(thing: Any) -> tuple[int, int, int] | None:
    """``(x, y, plane)`` of a tile, coordinate or entity row; None for anything else."""
    if thing is None:
        return None
    if hasattr(thing, "tile_x"):
        return int(thing.tile_x), int(thing.tile_y), int(thing.plane)
    if hasattr(thing, "x") and hasattr(thing, "y") and hasattr(thing, "plane"):
        return int(thing.x), int(thing.y), int(thing.plane)
    return None


@dataclass(frozen=True, slots=True)
class Coordinate:
    """A world tile ``(x, y, plane)``. The same value as :class:`Tile`, with distances, regions
    and offsets."""

    x: int
    y: int
    plane: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.plane <= MAX_PLANE:
            raise ValueError(f"plane must be 0..{MAX_PLANE}, got {self.plane}")

    @classmethod
    def of(cls, thing: Any) -> "Coordinate":
        """The coordinate of a tile, coordinate or entity row."""
        pos = position_of(thing)
        if pos is None:
            raise TypeError(f"{thing!r} has no tile position")
        return cls(*pos)

    @property
    def tile(self) -> Tile:
        return Tile(self.x, self.y, self.plane)

    def chebyshev_distance(self, other: Any) -> int:
        ox, oy, _ = _require(other)
        return max(abs(self.x - ox), abs(self.y - oy))

    distance_to = chebyshev_distance

    def euclidean_distance(self, other: Any) -> float:
        ox, oy, _ = _require(other)
        return math.hypot(self.x - ox, self.y - oy)

    @property
    def region_x(self) -> int:
        return self.x >> _REGION_SHIFT

    @property
    def region_y(self) -> int:
        return self.y >> _REGION_SHIFT

    @property
    def region_id(self) -> int:
        """``region_x << 8 | region_y``."""
        return self.region_x << _REGION_ID_X_SHIFT | self.region_y

    @property
    def region_local_x(self) -> int:
        return self.x & _REGION_LOCAL_MASK

    @property
    def region_local_y(self) -> int:
        return self.y & _REGION_LOCAL_MASK

    def offset(self, dx: int, dy: int, dplane: int = 0) -> "Coordinate":
        return Coordinate(self.x + dx, self.y + dy, self.plane + dplane)

    def with_plane(self, plane: int) -> "Coordinate":
        return Coordinate(self.x, self.y, plane)


def _require(thing: Any) -> tuple[int, int, int]:
    pos = position_of(thing)
    if pos is None:
        raise TypeError(f"{thing!r} has no tile position")
    return pos


class Area(ABC):
    """A set of tiles on one plane."""

    plane: int  # every tile of the area is on this plane

    @abstractmethod
    def contains_xy(self, x: int, y: int, plane: int) -> bool: ...

    @abstractmethod
    def random_tile(self, rng: _random.Random | None = None) -> Coordinate: ...

    def contains(self, thing: Any) -> bool:
        """Whether a tile, coordinate or entity row is inside. None is never inside."""
        pos = position_of(thing)
        return pos is not None and self.contains_xy(*pos)

    def __contains__(self, thing: Any) -> bool:
        return self.contains(thing)

    def bounding_box(self) -> "RectangularArea | None":
        return None

    def random_reachable_tile(self, walker: Any, attempts: int = DEFAULT_REACHABLE_ATTEMPTS,
                              rng: _random.Random | None = None) -> Coordinate | None:
        """A random tile the walker can reach (``walker.is_reachable``), trying ``attempts``
        draws; None when none of them was reachable. One pathfind per draw."""
        for _ in range(attempts):
            tile = self.random_tile(rng)
            if walker.is_reachable(tile.tile):
                return tile
        return None


def _rng(rng: _random.Random | None) -> Any:
    return rng if rng is not None else _random


@dataclass(frozen=True, slots=True)
class RectangularArea(Area):
    """The inclusive rectangle ``min_x..max_x`` x ``min_y..max_y`` on ``plane``."""

    min_x: int
    min_y: int
    max_x: int
    max_y: int
    plane: int = 0

    def __post_init__(self) -> None:
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError(f"min corner ({self.min_x},{self.min_y}) is past max corner "
                             f"({self.max_x},{self.max_y})")
        if not 0 <= self.plane <= MAX_PLANE:
            raise ValueError(f"plane must be 0..{MAX_PLANE}")

    @classmethod
    def of(cls, corner: Any, opposite: Any) -> "RectangularArea":
        ax, ay, ap = _require(corner)
        bx, by, bp = _require(opposite)
        if ap != bp:
            raise ValueError(f"corners are on planes {ap} and {bp}")
        return cls(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), ap)

    @classmethod
    def around(cls, centre: Any, radius: int) -> "RectangularArea":
        c = Coordinate.of(centre)
        return cls.of(c.offset(-radius, -radius), c.offset(radius, radius))

    def bounding_box(self) -> "RectangularArea":
        return self

    def contains_xy(self, x: int, y: int, plane: int) -> bool:
        return (plane == self.plane and self.min_x <= x <= self.max_x
                and self.min_y <= y <= self.max_y)

    def random_tile(self, rng: _random.Random | None = None) -> Coordinate:
        r = _rng(rng)
        return Coordinate(r.randint(self.min_x, self.max_x), r.randint(self.min_y, self.max_y),
                          self.plane)

    @property
    def width(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def height(self) -> int:
        return self.max_y - self.min_y + 1

    def to_avoid(self) -> Any:
        """This rectangle as the walker's avoid area (``botwithus.api.Area``) on its plane."""
        from ..core.walk import Area as AvoidArea
        return AvoidArea(self.min_x, self.min_y, self.max_x, self.max_y, self.plane)


@dataclass(frozen=True, slots=True)
class CircularArea(Area):
    """Every tile within Euclidean ``radius`` of ``centre``, on its plane."""

    centre: Coordinate
    radius: int

    def __post_init__(self) -> None:
        if self.radius < 0:
            raise ValueError(f"radius must be >= 0, got {self.radius}")

    @property
    def plane(self) -> int:
        return self.centre.plane

    def bounding_box(self) -> RectangularArea:
        return RectangularArea.around(self.centre, self.radius)

    def contains_xy(self, x: int, y: int, plane: int) -> bool:
        dx = x - self.centre.x
        dy = y - self.centre.y
        return plane == self.centre.plane and dx * dx + dy * dy <= self.radius * self.radius

    def random_tile(self, rng: _random.Random | None = None) -> Coordinate:
        bounds = self.bounding_box()
        while True:
            tile = bounds.random_tile(rng)
            if self.contains(tile):
                return tile


class PolygonalArea(Area):
    """A polygon on one plane. Tiles on its edges and vertices are inside; elsewhere the
    even-odd rule decides."""

    __slots__ = ("vertices", "_bounds")

    def __init__(self, vertices: Iterable[Any]) -> None:
        coords = tuple(Coordinate.of(v) for v in vertices)
        if len(coords) < MIN_POLYGON_VERTICES:
            raise ValueError(f"a polygon needs at least {MIN_POLYGON_VERTICES} vertices, "
                             f"got {len(coords)}")
        planes = {c.plane for c in coords}
        if len(planes) != 1:
            raise ValueError(f"vertices span planes {sorted(planes)}")
        self.vertices: tuple[Coordinate, ...] = coords
        self._bounds = RectangularArea(min(c.x for c in coords), min(c.y for c in coords),
                                       max(c.x for c in coords), max(c.y for c in coords),
                                       coords[0].plane)

    @classmethod
    def of(cls, *vertices: Any) -> "PolygonalArea":
        return cls(vertices)

    def __repr__(self) -> str:
        return f"PolygonalArea({list(self.vertices)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PolygonalArea) and other.vertices == self.vertices

    def __hash__(self) -> int:
        return hash(self.vertices)

    @property
    def plane(self) -> int:
        return self.vertices[0].plane

    def bounds(self) -> RectangularArea:
        return self._bounds

    def bounding_box(self) -> RectangularArea:
        return self._bounds

    def contains_xy(self, x: int, y: int, plane: int) -> bool:
        if not self._bounds.contains_xy(x, y, plane):
            return False
        return self._on_outline(x, y) or self._inside_even_odd(x, y)

    def random_tile(self, rng: _random.Random | None = None) -> Coordinate:
        for _ in range(_MAX_REJECTION_DRAWS):
            tile = self._bounds.random_tile(rng)
            if self.contains(tile):
                return tile
        tiles = self.tiles()
        return tiles[_rng(rng).randrange(len(tiles))]

    def tiles(self) -> list[Coordinate]:
        """Every tile inside, row by row."""
        b = self._bounds
        return [Coordinate(x, y, b.plane) for y in range(b.min_y, b.max_y + 1)
                for x in range(b.min_x, b.max_x + 1) if self.contains_xy(x, y, b.plane)]

    def _on_outline(self, x: int, y: int) -> bool:
        vs = self.vertices
        return any(_on_segment(x, y, vs[i], vs[(i + 1) % len(vs)]) for i in range(len(vs)))

    def _inside_even_odd(self, x: int, y: int) -> bool:
        inside = False
        vs: Sequence[Coordinate] = self.vertices
        j = len(vs) - 1
        for i in range(len(vs)):
            a, b = vs[i], vs[j]
            if (a.y > y) != (b.y > y):
                cross_x = a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y)
                if x < cross_x:
                    inside = not inside
            j = i
        return inside


def _on_segment(x: int, y: int, a: Coordinate, b: Coordinate) -> bool:
    cross = (b.x - a.x) * (y - a.y) - (b.y - a.y) * (x - a.x)
    return (cross == 0 and min(a.x, b.x) <= x <= max(a.x, b.x)
            and min(a.y, b.y) <= y <= max(a.y, b.y))
