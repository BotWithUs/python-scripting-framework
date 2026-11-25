"""Conversation dialogs: pick an option on the multi-choice dialog, continue an NPC chat page.

Mirrors the Java host's ``api/dialog/Dialog`` (JBotWithUsV2). Two interfaces:

* **1188**, "select an option". Its option rows are LAYER components hanging directly off
  component 0. The visible text is not on the row: each row's subtree holds a number cell
  ("1.") and the label ("What's wrong?") as TEXT leaves. The option is selected on the ROW
  with the DIALOGUE action (30). A COMPONENT click on it dispatches, but the server ignores it
  and the dialogue stays stuck.
* **1184**, a text-only NPC chat page. Its continue button is component 15, advanced with the
  same DIALOGUE action (what a manual click emits: ``30, 0, -1, (1184 << 16) | 15``).

Reading the options is one ``get_interface_tree`` round trip; the walk is in memory.

    dialog = Dialog(game)
    if dialog.is_open():
        dialog.select("What's wrong?")
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .actions import dialogue_option
from .gameval import GamevalRef, GamevalType, component_ref

if TYPE_CHECKING:
    from .game import GameApi

MULTI_CHOICE_IFACE = 1188
OPTIONS_CONTAINER = 0
NPC_CHAT_IFACE = 1184
NPC_CHAT_ROOT = 0
NPC_CHAT_CONTINUE = 15

# Stable component categories (PUBLIC_PROTOCOL.md 6.7.1 ``category``; Java ComponentType).
CATEGORY_LAYER = 1
CATEGORY_TEXT = 3

_NUMBER_CELL = re.compile(r"\d+\.")
_MARKUP = re.compile(r"<[^>]+>")


def normalize(text: str) -> str:
    """Lower case, markup (``<col=...>``) stripped, whitespace collapsed: how labels match."""
    return " ".join(_MARKUP.sub(" ", text or "").split()).casefold()


@dataclass(frozen=True, slots=True)
class DialogOption:
    """One selectable line. ``index`` is the 1-based number shown in game (its "N." cell, or
    the row's position when it has none). ``text`` is the label with its markup intact.
    ``iface``/``comp`` are the ROW the selection targets, not the text leaf."""
    index: int
    text: str
    iface: int
    comp: int


def _children(nodes: list[dict]) -> list[list[int]]:
    """Each node's child indices, in tree order, from the tree's ``parent`` links."""
    kids: list[list[int]] = [[] for _ in nodes]
    for i, node in enumerate(nodes):
        parent = node.get("parent", -1)
        if isinstance(parent, int) and 0 <= parent < len(nodes) and parent != i:
            kids[parent].append(i)
    return kids


def _descendants(index: int, kids: list[list[int]]) -> list[int]:
    """Depth-first, as the Java host's ``ComponentNode.descendants``."""
    out: list[int] = []
    stack = list(reversed(kids[index]))
    while stack:
        i = stack.pop()
        out.append(i)
        stack.extend(reversed(kids[i]))
    return out


def cells(texts: list[str], position: int) -> tuple[int, str] | None:
    """A row's ``(index, label)`` from its non-blank texts in tree order, or None with no label.

    The first text shaped like a number cell ("N.") is the index; every later text is the
    label, the last one winning, so a label that itself looks like "4." (a quiz answer) is kept.
    Without a number cell the index is ``position``."""
    label: str | None = None
    parsed = -1
    for text in texts:
        stripped = text.strip()
        if parsed < 0 and _NUMBER_CELL.fullmatch(stripped) and int(stripped[:-1]) > 0:
            parsed = int(stripped[:-1])
        else:
            label = text
    if label is None or not label.strip():
        return None
    return (parsed if parsed > 0 else position), label


class Dialog:
    """The dialog reads and selections for one game connection."""

    def __init__(self, game: "GameApi") -> None:
        self._game = game

    def options(self) -> list[DialogOption]:
        """The options shown now, in display order; empty when 1188 is not open."""
        tree = self._game.interface_tree(MULTI_CHOICE_IFACE, OPTIONS_CONTAINER)
        nodes = tree.get("nodes") if isinstance(tree, dict) else None
        if not isinstance(nodes, list) or not nodes:
            return []
        container = _find(nodes, MULTI_CHOICE_IFACE, OPTIONS_CONTAINER)
        if container is None:
            return []
        kids = _children(nodes)
        out: list[DialogOption] = []
        for row in kids[container]:
            if nodes[row].get("category") != CATEGORY_LAYER:
                continue
            texts = [str(nodes[i].get("text", "")) for i in _descendants(row, kids)
                     if nodes[i].get("category") == CATEGORY_TEXT
                     and str(nodes[i].get("text", "")).strip()]
            found = cells(texts, len(out) + 1)
            if found is not None:
                out.append(DialogOption(found[0], found[1], int(nodes[row]["iface"]),
                                        int(nodes[row]["comp"])))
        return out

    def is_open(self) -> bool:
        """The multi-choice dialog is open with at least one option."""
        return bool(self.options())

    def select(self, choice: str | int) -> bool:
        """Select by text (the first label containing it, compared with :func:`normalize`) or
        by 1-based index. True when an option matched and its row's DIALOGUE action was
        queued; False, with nothing sent, when none did."""
        if isinstance(choice, bool):
            raise TypeError("choice must be an option's text or its 1-based index")
        if isinstance(choice, int):
            match = next((o for o in self.options() if o.index == choice), None)
        else:
            needle = normalize(choice)
            if not needle:
                return False
            match = next((o for o in self.options() if needle in normalize(o.text)), None)
        if match is None:
            return False
        self._game.queue_action(dialogue_option(match.iface, match.comp))
        return True

    def is_chat_open(self) -> bool:
        """The text-only NPC chat page (1184) is open. 1184 unloads when the page closes, so
        its root resolving is an exact test."""
        found = self._game.component(NPC_CHAT_IFACE, NPC_CHAT_ROOT)
        return isinstance(found, dict) and found.get("iface", -1) != -1

    def continue_chat(self) -> bool:
        """Advance an NPC chat page. False, with nothing sent, when 1184 is not open."""
        if not self.is_chat_open():
            return False
        self._game.queue_action(dialogue_option(NPC_CHAT_IFACE, NPC_CHAT_CONTINUE))
        return True


    # --- chat pages (J3: Dialog.getText / getTitle / continueDialog) ------------------

    def open_page(self) -> "ChatPage | None":
        """The chat page shown now: NPC chat 1184, player chat 1191, message box 1186 or item
        box 1189 (in that order), or None."""
        snap = self._game.snapshot()
        return next((p for p in ChatPage if snap.is_interface_open(p.iface)), None)

    def is_player_chat_open(self) -> bool:
        return self._game.snapshot().is_interface_open(ChatPage.PLAYER.iface)

    def get_text(self) -> str | None:
        """The open chat page's text, markup intact; None with no page open. Use
        :func:`plain` to strip the markup."""
        page = self.open_page()
        return None if page is None else self._text_of(page.iface, page.text_comp)

    def get_title(self) -> str | None:
        """The speaker's name on an NPC or player chat page; None on a message or item box,
        or with no page open."""
        page = self.open_page()
        if page is None or page.title_comp is None:
            return None
        return self._text_of(page.iface, page.title_comp)

    def can_continue(self) -> bool:
        return self.open_page() is not None

    def continue_dialog(self) -> bool:
        """Press the open chat page's continue button (the DIALOGUE action). False, with
        nothing sent, when no page is open."""
        page = self.open_page()
        if page is None:
            return False
        self._game.queue_action(dialogue_option(page.iface, page.continue_comp))
        return True

    def _text_of(self, iface: int, comp: int) -> str | None:
        found = self._game.component(iface, comp)
        if not isinstance(found, dict) or found.get("iface", -1) == -1:
            return None
        return str(found.get("text") or "")


# Each page's interface gameval and its text / title / continue component names, as the Java
# host's ChatPage (native ABI 8). A component is "<interface>__<name>".
_CHAT_GAMEVALS: dict[str, tuple[str, str | None, str | None, str | None]] = {
    "NPC": ("CHAT_V2_LEFT", "CHAT_TEXT", "TITLE_TEXT", "CLICK_CONTINUE"),
    "PLAYER": ("CHAT_V2_RIGHT", "CHAT_TEXT", "TITLE_TEXT", "CLICK_CONTINUE"),
    "MESSAGE": ("MESBOX_V2", "MESBOX_TEXT", None, "CLICK_CONTINUE"),
    "OBJECT": ("OBJBOX_V2", "OBJBOX_TEXT", None, "CLICK_CONTINUE"),
}


class ChatPage(Enum):
    """``(iface, text comp, title comp or None, continue comp)``. Mirrors the Java host's
    ChatPage: CHAT_V2_LEFT, CHAT_V2_RIGHT, MESBOX_V2 and OBJBOX_V2 by gameval."""

    NPC = (NPC_CHAT_IFACE, 10, 4, NPC_CHAT_CONTINUE)
    PLAYER = (1191, 10, 4, 15)
    MESSAGE = (1186, 3, None, 8)
    OBJECT = (1189, 3, None, 20)

    @property
    def gameval(self) -> str:
        """The interface's gameval name (CHAT_V2_LEFT ...), as the Java host's ChatPage."""
        return _CHAT_GAMEVALS[self.name][0]

    def _component(self, index: int) -> int | None:
        names = _CHAT_GAMEVALS[self.name]
        if names[index] is None or self.value[index] is None:
            return None
        return component_ref(f"{names[0]}__{names[index]}", self.value[0], self.value[index]).component()[1]

    @property
    def iface(self) -> int:
        """The interface, by its gameval name; the fallback id when it does not resolve."""
        return GamevalRef(GamevalType.INTERFACE, self.gameval, self.value[0]).resolve()

    @property
    def text_comp(self) -> int:
        return self._component(1)  # type: ignore[return-value]

    @property
    def title_comp(self) -> int | None:
        return self._component(2)

    @property
    def continue_comp(self) -> int:
        return self._component(3)  # type: ignore[return-value]


def plain(text: str | None) -> str:
    """``text`` with markup removed and whitespace collapsed (case kept)."""
    return " ".join(_MARKUP.sub(" ", text or "").split())


CONFIRM_DESTROY_IFACE = 1183     # CONFIRM_DESTROY_V2
CONFIRM_DESTROY_YES = 7
CONFIRM_DESTROY_NO = 8
CONFIRM_DESTROY_NAME = 4
# The ids above are the fallbacks; these are the names the Java host resolves (native ABI 8).
CONFIRM_DESTROY_REF = GamevalRef(GamevalType.INTERFACE, "CONFIRM_DESTROY_V2", CONFIRM_DESTROY_IFACE)
CONFIRM_DESTROY_REFS = {
    CONFIRM_DESTROY_YES: component_ref("CONFIRM_DESTROY_V2__BUTTON_YES", CONFIRM_DESTROY_IFACE, CONFIRM_DESTROY_YES),
    CONFIRM_DESTROY_NO: component_ref("CONFIRM_DESTROY_V2__BUTTON_NO", CONFIRM_DESTROY_IFACE, CONFIRM_DESTROY_NO),
    CONFIRM_DESTROY_NAME: component_ref("CONFIRM_DESTROY_V2__DESTROY_NAME", CONFIRM_DESTROY_IFACE,
                                        CONFIRM_DESTROY_NAME),
}


class ConfirmItemDestruction:
    """The "destroy this item?" prompt (1183). Buttons are pressed with the DIALOGUE action."""

    def __init__(self, game: "GameApi") -> None:
        self._game = game

    def is_open(self) -> bool:
        return self._game.snapshot().is_interface_open(CONFIRM_DESTROY_REF.resolve())

    def item_name(self) -> str | None:
        if not self.is_open():
            return None
        found = self._game.component(*CONFIRM_DESTROY_REFS[CONFIRM_DESTROY_NAME].component())
        if not isinstance(found, dict) or found.get("iface", -1) == -1:
            return None
        return plain(str(found.get("text") or ""))

    def confirm(self) -> bool:
        return self._press(CONFIRM_DESTROY_YES)

    def cancel(self) -> bool:
        return self._press(CONFIRM_DESTROY_NO)

    def _press(self, comp: int) -> bool:
        if not self.is_open():
            return False
        self._game.queue_action(dialogue_option(*CONFIRM_DESTROY_REFS[comp].component()))
        return True


def _find(nodes: list[Any], iface: int, comp: int) -> int | None:
    for i, node in enumerate(nodes):
        if isinstance(node, dict) and node.get("iface") == iface and node.get("comp") == comp:
            return i
    return None
