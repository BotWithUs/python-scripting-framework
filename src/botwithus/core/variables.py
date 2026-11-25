"""Varp / varbit reads over the native host (``bwu_read_varps`` / ``bwu_read_varbits``).

The native host composes the agent's per-id state with the game cache's varp definitions and
hands back one record per id; this module only turns those records into
:class:`~botwithus.api.variables.VarpRead` / :class:`~botwithus.api.variables.VarbitRead`.

State and kind numbers are mapped through the native module's own ``VARP_*`` / ``VAR_KIND_*``
constants, by name, rather than restated here, so the two sides cannot drift apart silently.
A number outside that set is read as UNAVAILABLE / UNKNOWN: an out-of-contract state says
nothing about the varp.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..api.variables import VarbitRead, VarKind, VarpRead, VarpState
from .native import NativeUnavailable

_STATE_CONSTANTS = {
    "VARP_SET": VarpState.SET,
    "VARP_DEFAULT_NOT_SET_CLIENTSIDE": VarpState.DEFAULT_NOT_SET_CLIENTSIDE,
    "VARP_NO_SUCH_VARP": VarpState.NO_SUCH_VARP,
    "VARP_UNAVAILABLE": VarpState.UNAVAILABLE,
}
_KIND_CONSTANTS = {
    "VAR_KIND_INT": VarKind.INT,
    "VAR_KIND_LONG": VarKind.LONG,
    "VAR_KIND_STRING": VarKind.STRING,
    "VAR_KIND_UNKNOWN": VarKind.UNKNOWN,
}

DEFAULTS_READY = 1
DEFAULTS_WARMING = 0
DEFAULTS_NEVER = -1


def _number_map(host: Any, names: dict[str, Any]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for name, member in names.items():
        number = host.constant(name)
        if number is None:
            raise NativeUnavailable(
                f"this bwu_host has no {name}; it predates varp state reads -- update it")
        out[int(number)] = member
    return out


class VariableReader:
    """Reads varps and varbits for one attached client."""

    __slots__ = ("_host", "_states", "_kinds")

    def __init__(self, host: Any) -> None:
        self._host = host
        self._states: dict[int, VarpState] | None = None
        self._kinds: dict[int, VarKind] | None = None

    def _maps(self) -> tuple[dict[int, VarpState], dict[int, VarKind]]:
        if self._states is None or self._kinds is None:
            self._states = _number_map(self._host, _STATE_CONSTANTS)
            self._kinds = _number_map(self._host, _KIND_CONSTANTS)
        return self._states, self._kinds

    def read_varps(self, ids: Iterable[int]) -> list[VarpRead]:
        """One read per id, in order. Any number of ids (the host batches at the agent cap)."""
        states, kinds = self._maps()
        return [
            VarpRead(
                id=int(r["id"]),
                state=states.get(int(r["state"]), VarpState.UNAVAILABLE),
                value=int(r["value"]),
                value64=int(r["value64"]),
                kind=kinds.get(int(r["kind"]), VarKind.UNKNOWN),
                default_verified=bool(r["default_verified"]),
            )
            for r in self._host.read_varps(list(ids))
        ]

    def read_varbits(self, ids: Iterable[int]) -> list[VarbitRead]:
        """One read per id, in order."""
        states, _ = self._maps()
        return [
            VarbitRead(
                id=int(r["id"]),
                state=states.get(int(r["state"]), VarpState.UNAVAILABLE),
                value=int(r["value"]),
                default_verified=bool(r["default_verified"]),
            )
            for r in self._host.read_varbits(list(ids))
        ]

    def defaults_status(self) -> int:
        """:data:`DEFAULTS_READY`, :data:`DEFAULTS_WARMING` or :data:`DEFAULTS_NEVER`."""
        return self._host.varp_defaults_status()
