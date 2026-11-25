"""Skill type ids for the local player's skills array.

WHERE THESE NUMBERS COME FROM, and where they do NOT.

They are **not** read from the game cache. The agent publishes
``SkillEntry.type_id`` as the game's own ``StatType.id`` and does not publish the
name beside it, and there is no stat or skill gameval in the
``RuneScape Data Dump`` on this workstation -- checked, and the answer was a
negative: the dump has ``npc``, ``loc``, ``item``, ``enum``, ``struct``,
``param``, ``varbit`` and friends, but nothing keyed on stats. So the cache is
not available as a source and this table cannot be derived from data here.

The table is instead **mirrored from a sibling host that already ships it**:
``JBotWithUsV2 api/src/main/java/com/botwithus/bot/api/util/Skills.java:24-53``,
whose own comment reads "Skill type IDs (RS3 ordering -- matches the in-game
StatType.id)".

Two independent corroborations, so this is not one source trusted blindly:

1. A live read of a real account through ``bwu_host`` reported
   ``skill_count == 29``, which is exactly ``ATTACK..NECROMANCY`` inclusive --
   the same count and the same last entry as the Java table.
2. On that same account the highest skill by far was id 8 and id 9 sat at 31,
   which is the shape of a woodcutting-and-fletching character, consistent with
   ``WOODCUTTING = 8`` and ``FLETCHING = 9``.

That is strong, but it is still an ordering copied from another consumer rather
than resolved from the cache. If a skill is ever added or reordered, this table
and the Java one are both wrong until someone edits them. Treat a surprising
level as a reason to re-check the id before trusting it.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .xp import XpTable


class Skill(IntEnum):
    """RS3 skill type ids. ``IntEnum`` so it compares equal to a raw ``type_id``."""

    ATTACK = 0
    DEFENCE = 1
    STRENGTH = 2
    CONSTITUTION = 3
    RANGED = 4
    PRAYER = 5
    MAGIC = 6
    COOKING = 7
    WOODCUTTING = 8
    FLETCHING = 9
    FISHING = 10
    FIREMAKING = 11
    CRAFTING = 12
    SMITHING = 13
    MINING = 14
    HERBLORE = 15
    AGILITY = 16
    THIEVING = 17
    SLAYER = 18
    FARMING = 19
    RUNECRAFTING = 20
    HUNTER = 21
    CONSTRUCTION = 22
    SUMMONING = 23
    DUNGEONEERING = 24
    DIVINATION = 25
    INVENTION = 26
    ARCHAEOLOGY = 27
    NECROMANCY = 28

    # --- J1: display name, elite flag, xp table (JBotWithUsV2 util/SkillType) -----------

    @property
    def display_name(self) -> str:
        """The name the game shows ("Runecrafting")."""
        return self.name.capitalize()

    @property
    def is_elite(self) -> bool:
        """An elite skill levels on the elite xp table (Invention)."""
        return self is Skill.INVENTION

    @property
    def xp_table(self) -> "XpTable":
        from .xp import XpTable
        return XpTable.ELITE if self.is_elite else XpTable.STANDARD

    @classmethod
    def by_id(cls, skill_id: int) -> "Skill | None":
        """The skill with type id ``skill_id``, or None."""
        try:
            return cls(skill_id)
        except ValueError:
            return None


# The Java host's name for the same enum (util/SkillType).
SkillType = Skill
