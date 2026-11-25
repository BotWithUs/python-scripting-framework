"""The Skill id table.

These assertions cannot prove the ordering is right -- it is copied from a
sibling host, not resolved from the cache (see ``api/skills.py``). What they
pin is that the table stays the shape the evidence supports, so an edit that
shifts an id has to be a deliberate one.
"""
from botwithus.api.skills import Skill

# The count a live account reported through bwu_host, and the corroboration for
# the table ending at NECROMANCY rather than somewhere else.
LIVE_SKILL_COUNT = 29


def test_ids_are_contiguous_from_zero():
    assert [s.value for s in Skill] == list(range(len(Skill)))


def test_table_size_matches_the_live_skill_count():
    assert len(Skill) == LIVE_SKILL_COUNT


def test_first_and_last_anchor_the_ordering():
    assert Skill.ATTACK == 0
    assert Skill.NECROMANCY == LIVE_SKILL_COUNT - 1


def test_the_ids_this_project_actually_relies_on():
    """Woodcutting is the one Phase 2 gates on; Fletching corroborated it live."""
    assert Skill.WOODCUTTING == 8
    assert Skill.FLETCHING == 9


def test_compares_equal_to_a_raw_type_id():
    """SkillEntry.type_id is a plain int; IntEnum keeps the comparison honest."""
    assert Skill.WOODCUTTING == 8
    assert 8 in {int(s) for s in Skill}


def test_names_are_unique():
    assert len({s.name for s in Skill}) == len(Skill)
