"""Which way an entity is facing, as the client stores it.

The raw value is the client's own angle, ``0..16383`` per full turn: the entity's
*rendered* facing, interpolated while it turns (so it reads in-between angles for a few
ticks after a turn starts). It is :data:`UNKNOWN_RAW` when the producer could not read one.

Use :func:`degrees` (compass bearing, clockwise from north) or :func:`compass` (nearest of
eight points) for anything but an exact comparison. Both return ``None`` for an unknown
facing -- never "north" or ``0.0`` for a value that was not read.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum

_log = logging.getLogger(__name__)

FULL_TURN = 16384
"""Client angle units in one full turn."""

NORTH_RAW = 8192
"""The raw angle that faces north (+tileY). Raw grows clockwise seen from above:
``0`` south, ``4096`` west, ``8192`` north, ``12288`` east.

Inferred statically, not yet confirmed against a live client. If that check disagrees this
is the one line to change; ``tests/test_orientation.py`` pins what the cardinals must become.
"""

UNKNOWN_RAW = -1
"""The raw value for a facing that is not known."""

WIRE_UNKNOWN = 0xFFFF
"""What the producer publishes for "not known": an all-ones u16."""

READBACK_TOLERANCE = 1
"""How far, in raw units, a facing read back from the client may sit from the angle that set
it: the client's conversion truncates, so angle ``j`` can read back as ``j - 1``. Compare
facings with :func:`is_same_facing`, never ``==``."""

_DEGREES_PER_TURN = 360.0
_SECTOR_DEGREES = 45.0


class Direction(Enum):
    """The eight compass points, clockwise from north. Order is load-bearing:
    :func:`compass` indexes this enum by 45-degree sector."""

    NORTH = 0
    NORTH_EAST = 1
    EAST = 2
    SOUTH_EAST = 3
    SOUTH = 4
    SOUTH_WEST = 5
    WEST = 6
    NORTH_WEST = 7

    @property
    def degrees(self) -> float:
        """Compass bearing of this point, clockwise from north."""
        return self.value * _SECTOR_DEGREES


_POINTS = tuple(Direction)


def from_wire(wire_value: int, on_out_of_contract: Callable[[int], None]) -> int:
    """Decode the producer's zero-extended u16, degrading rather than raising.

    :data:`WIRE_UNKNOWN` is unknown and ``0..16383`` is the angle. Anything else breaks the
    producer's contract: it also decodes as :data:`UNKNOWN_RAW` (never wrapped into a
    plausible wrong angle) and is passed to ``on_out_of_contract``. This never raises -- it
    runs inside a snapshot decode, and one bad row must not take down every running script.
    Passing an out-of-range raw to :func:`degrees` / :func:`compass` still raises, because
    that is a caller's mistake rather than the wire's."""
    if wire_value == WIRE_UNKNOWN:
        return UNKNOWN_RAW
    if not _is_angle(wire_value):
        on_out_of_contract(wire_value)
        return UNKNOWN_RAW
    return wire_value


class WireOrientationDecoder:
    """:func:`from_wire` for the snapshot decode path, reporting a broken producer
    invariant at most once. The same bad value can repeat on many rows and ticks; one
    decoder per attached session reports only its first."""

    __slots__ = ("_report", "_has_reported")

    def __init__(self, report: Callable[[int], None] | None = None) -> None:
        self._report = report or _warn_out_of_contract
        self._has_reported = False

    def decode(self, wire_value: int) -> int:
        """Decode one zero-extended u16. Never raises."""
        return from_wire(wire_value, self._report_once)

    def _report_once(self, value: int) -> None:
        if not self._has_reported:
            self._has_reported = True
            self._report(value)


def _warn_out_of_contract(value: int) -> None:
    _log.warning("producer published orientation %d (neither 0..%d nor 0x%X); decoding it as "
                 "unknown. Further occurrences this session are not logged.",
                 value, FULL_TURN - 1, WIRE_UNKNOWN)


def is_known(raw: int) -> bool:
    return raw != UNKNOWN_RAW


def is_same_facing(a: int, b: int) -> bool:
    """True when both facings are known and within :data:`READBACK_TOLERANCE` of each other
    around the circle (``0`` and ``16383`` are one unit apart). Use this, not ``==``: the
    client can read an angle back one unit short. An unknown facing matches nothing."""
    if not (is_known(a) and is_known(b)):
        return False
    _check(a)
    _check(b)
    apart = (a - b) % FULL_TURN
    return min(apart, FULL_TURN - apart) <= READBACK_TOLERANCE


def degrees(raw: int) -> float | None:
    """Compass bearing in ``[0, 360)``, clockwise from north; ``None`` when unknown."""
    if not is_known(raw):
        return None
    _check(raw)
    return ((raw - NORTH_RAW) % FULL_TURN) * _DEGREES_PER_TURN / FULL_TURN


def compass(raw: int) -> Direction | None:
    """Nearest of the eight points; ``None`` when unknown. A bearing exactly on a sector
    boundary (22.5, 67.5, ...) rounds clockwise."""
    bearing = degrees(raw)
    if bearing is None:
        return None
    sector = int(bearing / _SECTOR_DEGREES + 0.5)  # half-up; Python's round() is half-even
    return _POINTS[sector % len(_POINTS)]


def _is_angle(value: int) -> bool:
    return 0 <= value < FULL_TURN


def _check(raw: int) -> None:
    if not _is_angle(raw):
        raise ValueError(f"orientation must be 0..{FULL_TURN - 1} or {UNKNOWN_RAW}: {raw}")
