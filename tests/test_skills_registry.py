"""SkillRegistry 的去重、查询与稳定排序。"""

from pathlib import Path

import pytest

from skills.errors import DuplicateSkill
from skills.models import Skill, SkillManifest
from skills.registry import SkillRegistry

KNOWN_TOOLS = frozenset({"read_file", "search_text", "list_files", "write_or_replace"})


def make_skill(sid: str, **overrides) -> Skill:
    data = {
        "id": sid,
        "version": "1.0.0",
        "description": "d",
        "task_types": ["investigation"],
        "trigger_keywords": ["k"],
        "required_tools": ["read_file"],
        "read_only": True,
        "max_prompt_chars": 100,
        "prompt_file": "prompt.md",
    }
    data.update(overrides)
    manifest = SkillManifest.from_dict(data, known_tools=KNOWN_TOOLS)
    return Skill(manifest=manifest, prompt="p", source=Path("/tmp") / sid)


def test_lookup_and_sorted_listing():
    reg = SkillRegistry([make_skill("b-skill"), make_skill("a-skill")])
    assert reg.get("a-skill") is not None
    assert reg.get("missing") is None
    assert reg.ids() == ("a-skill", "b-skill")
    assert [s.id for s in reg.list()] == ["a-skill", "b-skill"]
    assert "a-skill" in reg and len(reg) == 2


def test_duplicate_id_rejected():
    with pytest.raises(DuplicateSkill):
        SkillRegistry([make_skill("a-skill"), make_skill("a-skill")])
