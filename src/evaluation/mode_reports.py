"""三种 Agent 架构评测报告的身份校验、来源指纹与子集合并。"""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from evaluation.mode_holdout_tasks import ALL_TASKS

MODES = ("single", "workflow", "team")
COMPATIBILITY_FIELDS = (
    "models",
    "premium_model",
    "trials_per_task",
    "deadline_seconds",
    "max_steps",
)


def trial_identity(row: dict) -> tuple[str, str, int]:
    """解析受信 trial 身份；拒绝路径穿越、未知任务和布尔型 trial。"""
    mode, task, trial = row.get("mode"), row.get("task"), row.get("trial")
    if mode not in MODES or task not in ALL_TASKS or type(trial) is not int or not 1 <= trial <= 5:
        raise ValueError("无效的 trial 身份")
    return mode, task, trial


def load_report_root(root: Path) -> tuple[dict, Path, str]:
    """读取批次根目录的 report.json，并返回原始文件 SHA256 作为不可变指纹。"""
    path = (root / "report.json").resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 report.json：{error}") from error
    if not isinstance(report, dict):
        raise ValueError("report.json 顶层必须是 JSON 对象")
    return report, path, sha256(raw).hexdigest()


def validate_compatible_runs(source: dict, replacement: dict) -> None:
    """保证补跑只改变 trial 结果，不引入模型与执行预算之外的实验变量。"""
    for field in COMPATIBILITY_FIELDS:
        if source.get(field) != replacement.get(field):
            raise ValueError(f"合并报告的 {field} 不一致")

    source_modes = source.get("modes")
    replacement_modes = replacement.get("modes")
    source_tasks = source.get("tasks")
    replacement_tasks = replacement.get("tasks")
    if not isinstance(source_modes, list) or not isinstance(replacement_modes, list):
        raise ValueError("合并报告缺少 modes 列表")
    if not isinstance(source_tasks, list) or not isinstance(replacement_tasks, list):
        raise ValueError("合并报告缺少 tasks 列表")
    if not set(replacement_modes) <= set(source_modes):
        raise ValueError("替换报告 modes 必须是来源子集")
    if not set(replacement_tasks) <= set(source_tasks):
        raise ValueError("替换报告 tasks 必须是来源子集")


def _identity_map(report: dict, label: str) -> tuple[list[dict], dict[tuple[str, str, int], dict]]:
    """保持报告原顺序，同时检测会破坏一对一替换关系的重复身份。"""
    rows = report.get("trials")
    if not isinstance(rows, list):
        raise ValueError(f"{label}报告缺少 trials")
    identities = [trial_identity(row) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label}报告包含重复 trial 身份")
    return rows, dict(zip(identities, rows, strict=True))


def merge_subset(
    source: dict,
    replacement: dict,
    *,
    regrade: Callable[[dict], dict],
) -> tuple[list[dict], dict]:
    """按稳定身份替换非空子集，并用调用方提供的当前评分器重评所有保留项。"""
    validate_compatible_runs(source, replacement)
    source_rows, source_map = _identity_map(source, "来源")
    replacement_rows, replacement_map = _identity_map(replacement, "替换")
    if not replacement_rows:
        raise ValueError("替换报告 trials 不能为空")
    unknown = set(replacement_map) - set(source_map)
    if unknown:
        raise ValueError(f"替换报告包含来源之外的 trial 身份：{sorted(unknown)}")

    merged = []
    for row in source_rows:
        identity = trial_identity(row)
        merged.append(replacement_map[identity] if identity in replacement_map else regrade(row))

    identities = sorted(replacement_map)
    metadata = {
        "replacement_trials": len(replacement_rows),
        "retained_trials_regraded": len(source_rows) - len(replacement_rows),
        "replaced_identities": [
            {"mode": mode, "task": task, "trial": trial} for mode, task, trial in identities
        ],
    }
    return merged, metadata
