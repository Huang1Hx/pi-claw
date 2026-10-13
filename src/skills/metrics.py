"""从脱敏 Trace 事件聚合 Skill 观测指标；只读元数据，不读取提示正文。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SKILL_EVENTS = (
    "skill_discovered",
    "skill_selected",
    "skill_rejected",
    "skill_started",
    "skill_completed",
    "skill_failed",
)


def count_events(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    """统计各 Skill 事件出现次数，未知事件忽略。"""
    counts = {name: 0 for name in SKILL_EVENTS}
    for event in events:
        name = event.get("event") if isinstance(event, dict) else None
        if name in counts:
            counts[name] += 1
    return counts


def lifecycle_completeness(events: Iterable[dict[str, Any]]) -> float:
    """返回 started 中已收敛为 completed/failed 的比例；无 started 时视为 1.0。"""
    started = 0
    finished = 0
    for event in events:
        name = event.get("event") if isinstance(event, dict) else None
        if name == "skill_started":
            started += 1
        elif name in {"skill_completed", "skill_failed"}:
            finished += 1
    if started == 0:
        return 1.0
    return min(1.0, finished / started)
