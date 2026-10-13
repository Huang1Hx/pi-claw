"""确定性 Skill 选择：固定优先级，不调用 LLM。

优先级：用户显式选择 > 精确任务类型 > 确定性关键词评分 > 不选择。
匹配不明确（并列或低于阈值）时不激活，交回原有 Agent 流程。
"""

from __future__ import annotations

from skills.errors import SkillNotFound
from skills.models import SelectionMethod, Skill, SkillSelection
from skills.registry import SkillRegistry

# 关键词评分阈值：至少命中一个不同的触发词才认为相关。
MIN_KEYWORD_SCORE = 1


class SkillSelector:
    """基于注册表的纯函数式选择器；同输入必得同结果。"""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def select(
        self,
        task: str,
        *,
        explicit_id: str | None = None,
        task_type: str | None = None,
    ) -> SkillSelection:
        """按固定优先级选择 Skill；不满足条件时返回 NONE。"""
        if not isinstance(task, str):
            raise TypeError("task 必须是字符串")

        if explicit_id is not None:
            skill = self.registry.get(explicit_id)
            if skill is None:
                raise SkillNotFound(f"未知 Skill ID：{explicit_id}")
            return SkillSelection(skill, SelectionMethod.EXPLICIT, f"用户显式选择 {explicit_id}")

        if task_type is not None:
            candidates = [
                skill for skill in self.registry.list() if task_type in skill.manifest.task_types
            ]
            if len(candidates) == 1:
                skill = candidates[0]
                return SkillSelection(
                    skill, SelectionMethod.TASK_TYPE, f"任务类型精确匹配 {task_type}"
                )

        return self._keyword_select(task)

    def _keyword_select(self, task: str) -> SkillSelection:
        """按不同触发词命中数评分；唯一最高分且达到阈值才激活。"""
        haystack = task.casefold()
        scored: list[tuple[int, Skill]] = []
        for skill in self.registry.list():
            hits = {
                keyword
                for keyword in skill.manifest.trigger_keywords
                if keyword.casefold() in haystack
            }
            if hits:
                scored.append((len(hits), skill))
        if not scored:
            return SkillSelection(None, SelectionMethod.NONE, "无任务类型或关键词匹配")
        best = max(score for score, _ in scored)
        winners = [skill for score, skill in scored if score == best]
        if best < MIN_KEYWORD_SCORE or len(winners) != 1:
            return SkillSelection(None, SelectionMethod.NONE, "关键词评分并列或低于阈值")
        skill = winners[0]
        return SkillSelection(
            skill, SelectionMethod.KEYWORD, f"关键词评分 {best} 唯一最高：{skill.id}"
        )
