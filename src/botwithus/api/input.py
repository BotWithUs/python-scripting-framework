"""Typing into the game's input dialog through synthetic key triggers.

The game's text-entry prompt ("Enter amount:", "Enter name of friend...") is MESLAYER
interface 1469. Its typed-key handler is the type-10 (key) CS2 trigger on component
**1469:4** (MESLAYER__MES_TEXT2); component 0 has no triggers at all. That trigger exists
only while the dialog is open, so a key fired at a closed dialog is dropped by the agent.

A key is one ``ComponentTrigger`` (5003) action whose ``param3`` is a packed
:class:`KeyStroke`, ``(keyCode << 16) | keyChar``: ``keyCode`` a signed 16-bit Jagex key
code (``NO_KEY_CODE`` for none), ``keyChar`` a CP1252 character (identity for ASCII) or 0.
The dialog reads printable characters from the char half -- ``(-1, c)`` appends ``c`` -- and
control keys from the code half: ``ENTER`` (84, 0) submits, ``BACKSPACE`` (85, 0) deletes,
``ESCAPE`` (13, 0) cancels. So typing ``"3"`` and submitting is ``(-1, '3')`` then ``(84, 0)``.

The agent dispatches one queued action per server tick (~600 ms), so N characters and Enter
take N+1 ticks. They are queued in one batch so they cost one round trip and nothing else
lands between them.

The dialog's mode is varc 5 (``VARC_INPUT_MODE``): 7 for an amount, 2 for a name, 0 when
closed. :class:`InputDialog` checks the mode before it sends anything and returns False when
the dialog cannot take the input. Text the dialog would reject raises :class:`InputRejected`.
Nothing is sent in either case.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from .actions import ActionType, GameAction
from .game import MAX_ACTION_BATCH, GameApi

NO_KEY_CODE = -1
TRIGGER_TYPE_KEY = 10
SUB_NONE = -1

INPUT_IFACE = 1469
INPUT_COMP = 4
VARC_INPUT_MODE = 5
VARC_INPUT_TEXT = 2506

AMOUNT_MAX_LEN = 10
NAME_MAX_LEN = 12
# The most actions one queue_actions call takes; the agent refuses a larger batch whole.
MAX_BATCH = MAX_ACTION_BATCH

_U16_MAX = 0xFFFF
_I16_MIN, _I16_MAX = -0x8000, 0x7FFF
_I32_MAX = 0x7FFFFFFF
# What the agent reads for a varc the client holds no value for.
_VARC_UNSET = -1
_PRINTABLE_FIRST, _PRINTABLE_LAST = 0x20, 0x7E
# Live on 950-1: a submitted "1M" and "1m" both mean a million.
_AMOUNT_RE = re.compile(r"[0-9]+[kKmM]?")
_AMOUNT_MULTIPLIER = {"k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}
_NAME_RE = re.compile(r"[A-Za-z0-9 _\-.!]+")


class InputMode(IntEnum):
    """What the open dialog is asking for (varc 5). ``OTHER`` is any value not listed."""
    OTHER = -1
    CLOSED = 0
    NAME = 2
    AMOUNT = 7


class InputRejected(ValueError):
    """The argument is invalid: the dialog would reject it. Nothing was sent."""


class InputDialogError(RuntimeError):
    """The agent queued only part of a batch. Only an agent older than NXTLibrary #67 does
    this; a current one queues the whole batch or refuses it with
    :class:`~botwithus.api.game.ActionsRefused`, and then nothing was typed."""


def _to_i32(value: int) -> int:
    """Reinterpret an unsigned 32-bit pattern as the signed int the wire carries."""
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value > _I32_MAX else value


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} {value!r} is not an int in {low}..{high}")


@dataclass(frozen=True, slots=True)
class KeyStroke:
    """One key event: a Jagex key code (``NO_KEY_CODE`` for none) and a CP1252 char (0 for none)."""
    key_code: int
    key_char: int = 0

    def __post_init__(self) -> None:
        _check_range("key_code", self.key_code, _I16_MIN, _I16_MAX)
        _check_range("key_char", self.key_char, 0, _U16_MAX)

    def packed(self) -> int:
        """``(key_code << 16) | key_char`` as the signed int32 the trigger's arg carries."""
        return _to_i32(((self.key_code & _U16_MAX) << 16) | self.key_char)

    @staticmethod
    def character(c: str) -> "KeyStroke":
        """The char event ``(NO_KEY_CODE, c)`` for one printable ASCII character."""
        if len(c) != 1 or not _PRINTABLE_FIRST <= ord(c) <= _PRINTABLE_LAST:
            raise InputRejected(f"{c!r} is not one printable ASCII character")
        return KeyStroke(NO_KEY_CODE, ord(c))


ENTER = KeyStroke(84)
BACKSPACE = KeyStroke(85)
ESCAPE = KeyStroke(13)


def _trigger(iface: int, comp: int, trigger_type: int, arg: int, sub: int) -> GameAction:
    _check_range("iface", iface, 0, _U16_MAX)
    _check_range("comp", comp, 0, _U16_MAX)
    _check_range("sub", sub, _I16_MIN, _I16_MAX)
    return GameAction(
        ActionType.COMPONENT_TRIGGER,
        _to_i32((iface << 16) | comp),
        _to_i32((trigger_type << 16) | (sub & _U16_MAX)),
        _to_i32(arg),
    )


def component_trigger(iface: int, comp: int, trigger_type: int, arg: int = 0,
                      sub: int = SUB_NONE) -> GameAction:
    """Fire any CS2 trigger type on a component.

    For a key trigger (type 10) an ``arg`` whose high half is zero is taken as a bare Jagex
    key code and sent as ``arg << 16``, so 84 is Enter rather than the character 'T'. An arg
    with a non-zero high half is an already packed :meth:`KeyStroke.packed` and passes through.
    """
    _check_range("trigger_type", trigger_type, 0, _I16_MAX)
    if trigger_type == TRIGGER_TYPE_KEY and 0 < arg <= _U16_MAX:
        arg <<= 16
    return _trigger(iface, comp, trigger_type, arg, sub)


def key_action(iface: int, comp: int, stroke: KeyStroke, sub: int = SUB_NONE) -> GameAction:
    """The action that sends one :class:`KeyStroke` to a component's key trigger."""
    return _trigger(iface, comp, TRIGGER_TYPE_KEY, stroke.packed(), sub)


def fire_keys(game: GameApi, iface: int, comp: int, strokes: list[KeyStroke]) -> int:
    """Send ``strokes`` in order as one batch. Returns how many the agent queued: all of them.
    A refused batch raises :class:`~botwithus.api.game.ActionsRefused` with none queued."""
    if len(strokes) > MAX_BATCH:
        raise ValueError(f"{len(strokes)} keys is more than one batch ({MAX_BATCH})")
    actions = [key_action(iface, comp, s) for s in strokes]
    return game.queue_actions(actions) if actions else 0


def type_text(game: GameApi, iface: int, comp: int, text: str) -> int:
    """Type printable ASCII ``text`` as one ``(NO_KEY_CODE, c)`` per character, without submitting."""
    return fire_keys(game, iface, comp, [KeyStroke.character(c) for c in text])


def validate_amount(amount: int | str) -> str:
    """The text to type for ``amount``: digits, optionally one k/K/m/M suffix, at most 10 chars.

    An int is typed as its digits. The value may not exceed 2^31-1: an amount the game
    cannot hold is rejected rather than left to overflow.
    """
    if isinstance(amount, bool) or not isinstance(amount, (int, str)):
        raise InputRejected(f"an amount must be an int or a string, not {amount!r}")
    text = str(amount)
    if not _AMOUNT_RE.fullmatch(text):
        raise InputRejected(f"amount {text!r} must be digits with at most one k/K/m/M suffix")
    if len(text) > AMOUNT_MAX_LEN:
        raise InputRejected(f"amount {text!r} is longer than {AMOUNT_MAX_LEN} characters")
    multiplier = _AMOUNT_MULTIPLIER.get(text[-1], 1)
    digits = text[:-1] if multiplier != 1 else text
    if int(digits) * multiplier > _I32_MAX:
        raise InputRejected(f"amount {text!r} is larger than {_I32_MAX}")
    return text


def validate_name(text: str) -> str:
    """``text`` if a name dialog would take it: 1..12 of letters, digits, space and ``_-.!``."""
    if not isinstance(text, str) or not _NAME_RE.fullmatch(text):
        raise InputRejected(
            f"name {text!r} may hold only ASCII letters, digits, space and _-.! and not be empty")
    if len(text) > NAME_MAX_LEN:
        raise InputRejected(f"name {text!r} is longer than {NAME_MAX_LEN} characters")
    return text


def _still_valid(validate, combined: str) -> bool:
    """Whether the text already typed plus a valid new input still obeys the mode's rules.

    Breaking them is a dialog-state failure, like the wrong mode, not a bad argument: the
    caller can clear() and retry."""
    try:
        validate(combined)
    except InputRejected:
        return False
    return True


class InputDialog:
    """The game's input dialog (MESLAYER 1469), read and typed into over one ``GameApi``.

    Methods that send return False, sending nothing, when the dialog is closed, in the wrong
    mode, or already holds text that the new input would push past the mode's rules.
    """

    def __init__(self, game: GameApi) -> None:
        self._game = game

    def mode_raw(self) -> int:
        """varc 5 as the game holds it: 0 closed, 2 name, 7 amount, anything else unlisted.

        It reads -1 until the dialog has first been opened this session (the client holds no
        value yet), and -1 means closed. A failed read raises the RPC error instead."""
        return self._game.varc_int(VARC_INPUT_MODE)

    def mode(self) -> InputMode:
        raw = self.mode_raw()
        if raw == _VARC_UNSET:
            return InputMode.CLOSED
        return InputMode(raw) if raw in InputMode._value2member_map_ else InputMode.OTHER

    def is_open(self) -> bool:
        return self.mode() is not InputMode.CLOSED

    def text(self) -> str:
        """What has been typed so far (varc 2506), or "" when the dialog is closed."""
        return self._game.varc_string(VARC_INPUT_TEXT) if self.is_open() else ""

    def enter_amount(self, amount: int | str) -> bool:
        """Type an amount (``500``, ``"10k"``, ``"5m"``) and submit it."""
        text = validate_amount(amount)
        if self.mode() is not InputMode.AMOUNT:
            return False
        if not _still_valid(validate_amount, self._typed() + text):
            return False
        return self._send([KeyStroke.character(c) for c in text] + [ENTER])

    def enter_text(self, text: str) -> bool:
        """Type a name and submit it."""
        validate_name(text)
        if self.mode() is not InputMode.NAME:
            return False
        if not _still_valid(validate_name, self._typed() + text):
            return False
        return self._send([KeyStroke.character(c) for c in text] + [ENTER])

    def submit(self) -> bool:
        return self.is_open() and self._send([ENTER])

    def cancel(self) -> bool:
        return self.is_open() and self._send([ESCAPE])

    def backspace(self, count: int = 1) -> bool:
        _check_range("count", count, 1, MAX_BATCH)
        return self.is_open() and self._send([BACKSPACE] * count)

    def clear(self) -> bool:
        """Delete everything typed so far: one Backspace per character of :meth:`text`."""
        if not self.is_open():
            return False
        typed = len(self._typed())
        return self._send([BACKSPACE] * typed) if typed else True

    def _typed(self) -> str:
        """varc 2506 without the open check (the caller has already made it)."""
        return self._game.varc_string(VARC_INPUT_TEXT)

    def _send(self, strokes: list[KeyStroke]) -> bool:
        queued = fire_keys(self._game, INPUT_IFACE, INPUT_COMP, strokes)
        if queued != len(strokes):
            raise InputDialogError(
                f"the agent queued {queued} of {len(strokes)} keys (an agent older than "
                "NXTLibrary #67, with a full queue); the dialog may hold partial input")
        return True
