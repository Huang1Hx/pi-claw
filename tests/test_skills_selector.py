"""确定性 Skill 选择：固定优先级、零 LLM、可重复。"""

from pathlib import Path

import pytest

from skills.errors import SkillNotFound
from skills.models import SelectionMethod, Skill, SkillManifest
from skills.registry import SkillRegistry
from skills.selector import SkillSelector

KNOWN_TOOLS = frozenset({"read_file", "search_text", "list_files", "write_or_replace"})


def make_skill(sid, task_types, keywords) -> Skill:
    manifest = SkillManifest.from_dict(
        {
            "id": sid,
            "version": "1.0.0",
            "description": "d",
            "task_types": task_types,
            "trigger_keywords": keywords,
            "required_tools": ["read_file"],
            "read_only": True,
            "max_prompt_chars": 100,
            "prompt_file": "prompt.md",
        },
        known_tools=KNOWN_TOOLS,
    )
    return Skill(manifest=manifest, prompt="p", source=Path("/tmp") / sid)


@pytest.fixture
def selector():
    reg = SkillRegistry(
        [
            make_skill("repository-investigation", ["investigation"], ["定位", "调用链", "排查"]),
            make_skill("python-test-repair", ["test_repair"], ["修复测试", "测试失败"]),
        ]
    )
    return SkillSelector(reg)


def test_explicit_selection_wins(selector):
    sel = selector.select("随便什么任务", explicit_id="python-test-repair")
    assert sel.method is SelectionMethod.EXPLICIT
    assert sel.skill.id == "python-test-repair"


def test_unknown_explicit_id_raises(selector):
    with pytest.raises(SkillNotFound):
        selector.select("任务", explicit_id="nope")


def test_task_type_unique_match(selector):
    sel = selector.select("处理一下", task_type="test_repair")
    assert sel.method is SelectionMethod.TASK_TYPE
    assert sel.skill.id == "python-test-repair"


def test_keyword_unique_match(selector):
    sel = selector.select("帮我修复测试里的失败")
    assert sel.method is SelectionMethod.KEYWORD
    assert sel.skill.id == "python-test-repair"


def test_no_match_returns_none(selector):
    sel = selector.select("给 README 加一段介绍")
    assert sel.method is SelectionMethod.NONE
    assert sel.skill is None


def test_keyword_tie_returns_none():
    reg = SkillRegistry(
        [
            make_skill("skill-a", ["t"], ["共享词"]),
            make_skill("skill-b", ["t"], ["共享词"]),
        ]
    )
    sel = SkillSelector(reg).select("包含共享词的请求")
    assert sel.method is SelectionMethod.NONE and sel.skill is None


def test_task_type_ambiguous_falls_through_to_keyword():
    reg = SkillRegistry(
        [
            make_skill("skill-a", ["shared"], ["独有a"]),
            make_skill("skill-b", ["shared"], ["独有b"]),
        ]
    )
    sel = SkillSelector(reg).select("请求含 独有a", task_type="shared")
    assert sel.skill.id == "skill-a" and sel.method is SelectionMethod.KEYWORD


def test_repeated_selection_is_deterministic(selector):
    first = selector.select("修复测试失败的问题")
    for _ in range(5):
        again = selector.select("修复测试失败的问题")
        assert again.method is first.method
        assert (again.skill.id if again.skill else None) == (
            first.skill.id if first.skill else None
        )
        assert again.reason == first.reason
