"""Decode native-host event records into typed events.

The seq-based ring drain (head load, per-slot seq compare, drop detection) now happens in
the native host -- see ``host_surface.cpp`` ``DrainEvents``, which follows the algorithm this
module used to implement in Python. What is left here is the *body* decode: the native host
hands back already-committed ``{"type", "body"}`` records in order, and this turns each one
into a typed :mod:`botwithus.api.events` object. The body layouts are unchanged, so the
per-type ``struct`` parsing is byte-for-byte what it always was.

Each reader keeps a local monotonic counter used as the event ``seq`` (ordering id); the
ring's own seq is not exposed across the raw seam, and a per-attach counter serves the same
ordering/identity role for the API objects.
"""
from __future__ import annotations

import logging
import struct
from enum import IntEnum
from typing import Any, TypeVar

from . import layout as L
from ..api import events as E


# ActionRefusedBody (Events.h): int32 actionId, param1, param2, param3; uint32 reason,
# anchorCase, heldMs. 28 bytes.
_ACTION_REFUSED = struct.Struct("<iiiiIII")
# ObjVarChangeBody (Events.h): int32 invId, slot, varId, oldValue, newValue. 20 bytes.
_OBJ_VAR_CHANGE = struct.Struct("<iiiii")
# KeyInputBody (Events.h): uint32 key; uint8 isAlt, isCtrl, isShift, flags. 8 bytes.
_KEY_INPUT = struct.Struct("<IBBBB")

_log = logging.getLogger("botwithus.events")

_Enum = TypeVar("_Enum", bound=IntEnum)


def _enum_or_int(cls: type[_Enum], value: int) -> _Enum | int:
    """The member for ``value``, or ``value`` itself when the agent sent a newer one."""
    try:
        return cls(value)
    except ValueError:
        return value


class EventRingReader:
    """A cursor over one native host's event stream."""

    def __init__(self, host: Any) -> None:
        self._host = host
        self._seq = 0
        self.missed = 0  # kept for API compatibility; drops are counted in the native host

    def poll(self) -> list[E.GameEvent]:
        """Decode everything the native host drained since the last poll."""
        out: list[E.GameEvent] = []
        for rec in self._host.poll_events_raw():
            evt = decode_event(int(rec["type"]), rec["body"], self._seq)
            self._seq += 1
            if evt is not None:
                out.append(evt)
        return out


def _decode_key_input(body: bytes, seq: int) -> E.KeyInput | None:
    """A KeyInputBody. A short body is skipped; bytes past the eighth are ignored."""
    if len(body) < _KEY_INPUT.size:
        # A truncated body: skip it rather than decode a guess. Every agent writes 8 bytes,
        # so this is a layout disagreement, not an older agent.
        _log.debug("dropped a KeyInput event: body is %d bytes, expected %d",
                   len(body), _KEY_INPUT.size)
        return None
    key, is_alt, is_ctrl, is_shift, flags = _KEY_INPUT.unpack_from(body, 0)
    return E.KeyInput(seq, key, bool(is_alt), bool(is_ctrl), bool(is_shift),
                      synthetic=bool(flags & L.KEY_FLAG_SYNTHETIC),
                      text_input_focused=bool(flags & L.KEY_FLAG_TEXT_INPUT_FOCUSED),
                      is_repeat=bool(flags & L.KEY_FLAG_REPEAT))


def _decode_obj_var_change(body: bytes, seq: int) -> E.ObjVarChanged | None:
    """An ObjVarChangeBody. A short body is skipped; bytes past the twentieth are ignored."""
    if len(body) < _OBJ_VAR_CHANGE.size:
        _log.debug("dropped an ObjVarChange event: body is %d bytes, expected %d",
                   len(body), _OBJ_VAR_CHANGE.size)
        return None
    inv_id, slot, var_id, old, new = _OBJ_VAR_CHANGE.unpack_from(body, 0)
    return E.ObjVarChanged(seq, inv_id, slot, var_id, old, new)


def decode_event(etype: int, body: bytes, seq: int) -> E.GameEvent | None:
    """Turn one ``(type, body)`` record into a typed event, or ``None`` if unknown."""
    if etype == L.EVT_LOGIN_STATE_CHANGE:
        old, new = struct.unpack_from("<ii", body, 0)
        return E.LoginStateChanged(seq, old, new)
    if etype == L.EVT_TICK:
        (tick,) = struct.unpack_from("<i", body, 0)
        return E.ServerTick(seq, tick)
    if etype == L.EVT_VARP_CHANGE:
        vid, old, new = struct.unpack_from("<iii", body, 0)
        return E.VarpChanged(seq, vid, old, new)
    if etype == L.EVT_VARBIT_CHANGE:
        vid, old, new = struct.unpack_from("<iii", body, 0)
        return E.VarbitChanged(seq, vid, old, new)
    if etype == L.EVT_VARC_CHANGE:
        vid, old, new = struct.unpack_from("<iii", body, 0)
        return E.VarcChanged(seq, vid, old, new)
    if etype == L.EVT_OBJ_VAR_CHANGE:
        return _decode_obj_var_change(body, seq)
    if etype == L.EVT_CHAT_MESSAGE:
        msg_type, sender_len, text_len = struct.unpack_from("<iHH", body, 0)
        buf = body[8:]
        sender = buf[:sender_len].decode("utf-8", "replace")
        text = buf[sender_len:sender_len + text_len].decode("utf-8", "replace")
        return E.ChatMessage(seq, msg_type, sender, text)
    if etype == L.EVT_KEY_INPUT:
        return _decode_key_input(body, seq)
    if etype == L.EVT_ACTION_EXECUTED:
        a, p1, p2, p3 = struct.unpack_from("<iiii", body, 0)
        return E.ActionExecuted(seq, a, p1, p2, p3)
    if etype == L.EVT_ACTION_REFUSED:
        if len(body) < _ACTION_REFUSED.size:
            # A truncated body: skip it rather than decode a guess.
            _log.debug("dropped an ActionRefused event: body is %d bytes, expected %d",
                       len(body), _ACTION_REFUSED.size)
            return None
        a, p1, p2, p3, reason, anchor, held = _ACTION_REFUSED.unpack_from(body, 0)
        return E.ActionRefused(seq, a, p1, p2, p3, _enum_or_int(E.RefuseReason, reason),
                               _enum_or_int(E.AnchorCase, anchor), held)
    if etype == L.EVT_BREAK_STARTED:
        duration, _pad, fatigue, risk = struct.unpack_from("<iIdd", body, 0)
        return E.BreakStarted(seq, duration, fatigue, risk)
    if etype == L.EVT_BREAK_ENDED:
        return E.BreakEnded(seq)
    if etype == L.EVT_HITMARK:
        idx, ttype = struct.unpack_from("<ib", body, 0)
        hmtype, damage, cycle = struct.unpack_from("<iii", body, 8)
        return E.Hitmark(seq, idx, ttype, hmtype, damage, cycle)
    if etype == L.EVT_HEADBAR:
        idx, ttype = struct.unpack_from("<ib", body, 0)
        hbtype, value = struct.unpack_from("<ii", body, 8)
        return E.Headbar(seq, idx, ttype, hbtype, value)
    if etype == L.EVT_SPOT_ANIM:
        idx, ttype = struct.unpack_from("<ib", body, 0)
        spot_id, tx, ty, plane = struct.unpack_from("<ihhb", body, 8)
        return E.SpotAnim(seq, idx, ttype, spot_id, tx, ty, plane)
    # Unknown / reserved (token refresh, radio-group) -> skip.
    return None
