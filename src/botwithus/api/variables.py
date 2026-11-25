"""Varp and varbit reads that say *why* a value is what it is.

Varps are set lazily by the server, so a varp at its default usually has no client-side entry
at all. A value alone cannot tell you whether a varp is set: a set varp can hold ``-1``, and so
does an object-typed varp at its default. Every read therefore carries a :class:`VarpState`:

- :attr:`VarpState.SET` -- the client holds a value; ``value`` is it.
- :attr:`VarpState.DEFAULT_NOT_SET_CLIENTSIDE` -- the varp exists and the client holds no
  entry; ``value`` is the default the game reads (usually ``0``; ``-1`` for BOOLEAN-domain and
  object-typed varps). This is the normal state of most varps.
- :attr:`VarpState.NO_SUCH_VARP` -- the cache has no such varp (for a varbit: no such varbit).
- :attr:`VarpState.UNAVAILABLE` -- the read could not be made: not in the world, still entering
  it, a bad id, a timeout. It says nothing about the varp.

Decide on ``state``, never on ``value``. The defaults come from the game cache through the
native host; when it cannot confirm one (an older ``NXTCache.dll``, still warming up, a failed
lookup) the read is still ``DEFAULT_NOT_SET_CLIENTSIDE`` with ``value`` ``0`` and
``default_verified`` false.

A varbit takes its base variable's state and decodes the base's value -- for an unset base, its
default, so over a base whose default is ``-1`` every bit reads as set, exactly as the game
reads it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

NO_VALUE = -1
"""``value`` of a read that has none (NO_SUCH_VARP, UNAVAILABLE). A SET varp can hold it too."""


class VarpState(Enum):
    """What a varp or varbit read found."""

    SET = "set"
    DEFAULT_NOT_SET_CLIENTSIDE = "default_not_set_clientside"
    NO_SUCH_VARP = "no_such_varp"
    UNAVAILABLE = "unavailable"


class VarKind(Enum):
    """How a SET varp's value is stored. UNKNOWN for every other state and for varbits."""

    INT = "int"
    LONG = "long"
    STRING = "string"
    UNKNOWN = "unknown"


_HAS_VALUE = (VarpState.SET, VarpState.DEFAULT_NOT_SET_CLIENTSIDE)


@dataclass(frozen=True, slots=True)
class VarpRead:
    """One varp read.

    ``value`` is the low 32 bits (signed) of a LONG varp; ``value64`` is the whole value, and
    for any other varp is ``value`` sign-extended. Both are :data:`NO_VALUE` when there is no
    value. ``default_verified`` is false only for a default the cache could not confirm.
    """

    id: int
    state: VarpState
    value: int
    value64: int
    kind: VarKind
    default_verified: bool

    @property
    def is_set(self) -> bool:
        """True when the client holds a value for this varp."""
        return self.state is VarpState.SET

    @property
    def has_value(self) -> bool:
        """True when ``value`` is what the game reads (SET, or at its default)."""
        return self.state in _HAS_VALUE


@dataclass(frozen=True, slots=True)
class VarbitRead:
    """One varbit read: the base variable's state and the decoded bit field."""

    id: int
    state: VarpState
    value: int
    default_verified: bool

    @property
    def is_set(self) -> bool:
        """True when the base variable holds a value client-side."""
        return self.state is VarpState.SET

    @property
    def has_value(self) -> bool:
        """True when ``value`` is what the game reads (SET, or decoded from a default)."""
        return self.state in _HAS_VALUE
