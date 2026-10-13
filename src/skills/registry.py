"""内置 Skill 注册表：去重、稳定排序与查询。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from skills.errors import DuplicateSkill
from skills.loader import load_builtin_skills
from skills.models import Skill
from tools.registry import known_tool_names

BUILTIN_SKILLS_DIR = Path(__file__).with_name("builtin")


def known_tools() -> frozenset[str]:
    """当前工具注册表已知的工具名，用作 Manifest 的 required_tools 白名单。"""
    return known_tool_names()


class SkillRegistry:
    """按 ID 索引 Skill；构造时拒绝重复 ID，列表按 ID 排序以保证确定性。"""

    def __init__(self, skills: Iterable[Skill]) -> None:
        self._by_id: dict[str, Skill] = {}
        for skill in skills:
            if skill.id in self._by_id:
                raise DuplicateSkill(f"重复的 Skill ID：{skill.id}")
            self._by_id[skill.id] = skill

    def get(self, skill_id: str) -> Skill | None:
        return self._by_id.get(skill_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def list(self) -> list[Skill]:
        return [self._by_id[sid] for sid in sorted(self._by_id)]

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    @classmethod
    def from_builtin(cls, builtin_root: str | Path | None = None) -> SkillRegistry:
        """从内置目录加载全部 Skill；目录缺失时返回空注册表。"""
        root = Path(builtin_root) if builtin_root is not None else BUILTIN_SKILLS_DIR
        return cls(load_builtin_skills(root, known_tools=known_tools()))
