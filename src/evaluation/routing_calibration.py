"""离线路由校准：在带标签的 simple/complex 数据集上度量规则分类与宿主安全网。"""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from routing.classifier import require_strong
from routing.intent import classify

LABELS = ("simple", "complex")


def default_calibration_path() -> Path:
    """返回随包版本化的默认校准数据集。"""

    resource = files("evaluation").joinpath("routing_calibration_cases.json")
    return Path(str(resource))


def load_calibration_suite(path: str | Path | None = None) -> list[dict[str, Any]]:
    """加载带标签样本，拒绝未知格式、重复 id 与非法标签；不做任何模型调用。"""

    suite_path = Path(path) if path is not None else default_calibration_path()
    try:
        value = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"校准数据集不是有效 JSON：{suite_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("校准数据集根节点必须是 JSON 对象")
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ValueError("只支持 schema_version=1 的校准数据集")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("校准数据集 name 必须是非空字符串")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("校准数据集 version 必须是正整数")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("校准数据集 cases 必须是非空数组")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("每个校准样本必须是 JSON 对象")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("校准样本 id 必须是非空字符串")
        if case_id in seen:
            raise ValueError(f"校准样本 id 重复：{case_id}")
        seen.add(case_id)
        task = raw.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"校准样本 {case_id} 的 task 必须是非空字符串")
        label = raw.get("label")
        if label not in LABELS:
            raise ValueError(f"校准样本 {case_id} 的 label 必须是 simple 或 complex")
        known = raw.get("known_downgrade", False)
        if not isinstance(known, bool):
            raise ValueError(f"校准样本 {case_id} 的 known_downgrade 必须是布尔值")
        if known and label != "complex":
            raise ValueError(f"校准样本 {case_id} 的 known_downgrade 只能用于 complex 标签")
        note = raw.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"校准样本 {case_id} 的 note 必须是字符串")
        result.append(
            {
                "id": case_id,
                "task": task,
                "label": label,
                "known_downgrade": known,
                "note": note,
            }
        )
    return result


def evaluate_routing_calibration(output: Path, suite: str | Path | None = None) -> dict[str, Any]:
    """离线度量 rules 策略决策与宿主安全网决策的准确率、错误降级与保守升级。"""

    cases = load_calibration_suite(suite)
    rows: list[dict[str, Any]] = []
    for case in cases:
        rules_simple, reason = classify(case["task"])
        rules_decision = "simple" if rules_simple else "complex"
        host_guard = require_strong(case["task"])
        guarded_decision = "complex" if host_guard else rules_decision
        false_downgrade = case["label"] == "complex" and rules_decision == "simple"
        rows.append(
            {
                "id": case["id"],
                "label": case["label"],
                "rules_decision": rules_decision,
                "guarded_decision": guarded_decision,
                "host_guard": host_guard,
                "reason": reason,
                "known_downgrade": case["known_downgrade"],
                "note": case["note"],
                "rules_correct": rules_decision == case["label"],
                "guarded_correct": guarded_decision == case["label"],
                "false_downgrade": false_downgrade,
                "false_escalation": case["label"] == "simple" and rules_decision == "complex",
                "unexpected_downgrade": false_downgrade and not case["known_downgrade"],
            }
        )
    total = len(rows)
    complex_cases = sum(1 for row in rows if row["label"] == "complex")
    rules_false_downgrades = sum(row["false_downgrade"] for row in rows)
    guarded_false_downgrades = sum(
        row["label"] == "complex" and row["guarded_decision"] == "simple" for row in rows
    )
    report = {
        "suite": "routing-calibration-v1",
        "real_model": False,
        "assumptions": (
            "离线、固定规则：只评估确定性 routing.intent.classify（rules 策略真实决策）与宿主"
            "routing.classifier.require_strong 安全网（guarded 决策），不调用真实模型、不设价格、"
            "不报告节省率。label 为人工标注的任务真实复杂度；"
            "complex→simple 记为错误降级（安全风险），"
            "simple→complex 记为保守升级（成本）。known_downgrade 标记已接受的规则盲点，"
            "rules_unexpected_downgrades 是必须为 0 的回归门。"
        ),
        "total_cases": total,
        "simple_cases": total - complex_cases,
        "complex_cases": complex_cases,
        "rules_accuracy": round(sum(row["rules_correct"] for row in rows) / total, 6),
        "rules_false_downgrades": rules_false_downgrades,
        "rules_known_downgrades": sum(
            row["false_downgrade"] and row["known_downgrade"] for row in rows
        ),
        "rules_unexpected_downgrades": sum(row["unexpected_downgrade"] for row in rows),
        "rules_false_downgrade_rate": round(rules_false_downgrades / complex_cases, 6),
        "rules_false_escalations": sum(row["false_escalation"] for row in rows),
        "guarded_accuracy": round(sum(row["guarded_correct"] for row in rows) / total, 6),
        "guarded_false_downgrades": guarded_false_downgrades,
        "guarded_false_downgrade_rate": round(guarded_false_downgrades / complex_cases, 6),
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
