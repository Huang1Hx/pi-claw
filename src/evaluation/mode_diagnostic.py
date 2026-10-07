"""Stage 3 对冻结 12 个失败身份的一次性诊断复测契约。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Final

from evaluation.mode_repair import RepairJob
from evaluation.mode_reports import trial_identity
from evaluation.report import read_trace

FROZEN_STAGE3_FAILURES: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("single", "inventory_reservation", 3, "deadline"),
    ("workflow", "event_deduplication", 3, "deadline"),
    ("team", "rate_limit_window", 3, "loop_detected"),
    ("single", "config_overlay", 1, "deadline"),
    ("workflow", "config_overlay", 1, "deadline"),
    ("team", "config_overlay", 1, "loop_detected"),
    ("single", "config_overlay", 2, "deadline"),
    ("single", "config_overlay", 3, "deadline"),
    ("workflow", "config_overlay", 3, "provider_error"),
    ("team", "versioned_update", 1, "unverified"),
    ("single", "versioned_update", 2, "deadline"),
    ("workflow", "versioned_update", 2, "deadline"),
)

_REQUEST_BUDGET_MARKERS: Final[tuple[str, ...]] = (
    "请求上限",
    "request limit",
    "request budget",
)


def _identity_status_map(rows: object) -> dict[tuple[str, str, int], str]:
    """把公开未恢复清单规范化；重复或缺失状态必须失败。"""
    if not isinstance(rows, list):
        raise ValueError("诊断来源缺少 unrecovered 清单")
    result: dict[tuple[str, str, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("诊断来源 unrecovered 项无效")
        try:
            identity = trial_identity(row)
        except ValueError as error:
            raise ValueError("诊断来源冻结身份无效") from error
        status = row.get("status")
        if identity in result or not isinstance(status, str):
            raise ValueError("诊断来源冻结身份重复或缺少终态")
        result[identity] = status
    return result


def _repair_entry_map(ledger: dict) -> dict[tuple[str, str, int], dict]:
    """索引完整修复账本；诊断只读最后候选，不新增修复轮次。"""
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ValueError("诊断来源 repair-ledger.json 缺少 entries")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("identity"), dict):
            raise ValueError("诊断来源 repair ledger identity 无效")
        identity = trial_identity(entry["identity"])
        if identity in result:
            raise ValueError("诊断来源 repair ledger 身份重复")
        result[identity] = entry
    return result


def select_diagnostic_jobs(source: dict, ledger: dict) -> list[RepairJob]:
    """只选择 Iteration 021 冻结的 12 个最终候选，任何身份漂移都拒绝。"""
    repair = source.get("repair")
    repair_source = source.get("repair_source")
    if not isinstance(repair, dict) or not isinstance(repair_source, dict):
        raise ValueError("诊断来源不是完整修复报告")
    post = repair.get("post_repair")
    if (
        not isinstance(post, dict)
        or post.get("strict_successes") != 78
        or post.get("strict_failures") != 12
        or repair_source.get("max_rounds") != 2
    ):
        raise ValueError("诊断来源不是冻结的 78/90 两轮修复基线")
    expected = {(mode, task, trial): status for mode, task, trial, status in FROZEN_STAGE3_FAILURES}
    observed = _identity_status_map(repair.get("unrecovered"))
    if observed != expected:
        raise ValueError("诊断来源与冻结身份或终态不一致")
    entries = _repair_entry_map(ledger)

    jobs = []
    for mode, task, trial, expected_status in FROZEN_STAGE3_FAILURES:
        identity = (mode, task, trial)
        entry = entries.get(identity)
        rounds = entry.get("rounds") if isinstance(entry, dict) else None
        if (
            not isinstance(rounds, list)
            or not 1 <= len(rounds) <= 2
            or entry.get("final_passed") is not False
        ):
            raise ValueError(f"冻结身份缺少两轮内最终候选：{identity}")
        row = rounds[-1]
        if not isinstance(row, dict) or trial_identity(row) != identity:
            raise ValueError(f"冻结身份最终候选不一致：{identity}")
        if row.get("passed") is True or row.get("leader_status") != expected_status:
            raise ValueError(f"冻结身份最终终态不一致：{identity}")
        workspace_value = row.get("workspace")
        traces_value = row.get("traces")
        if (
            not isinstance(workspace_value, str)
            or not isinstance(traces_value, list)
            or not all(isinstance(value, str) for value in traces_value)
        ):
            raise ValueError(f"冻结身份缺少工作区或 Trace：{identity}")
        workspace = Path(workspace_value).resolve()
        if not (workspace / "solution.py").is_file():
            raise ValueError(f"冻结身份缺少 solution.py：{identity}")
        jobs.append(
            RepairJob(
                mode,
                task,
                trial,
                workspace,
                tuple(Path(value).resolve() for value in traces_value),
                dict(row),
            )
        )
    return jobs


def build_diagnostic_prompt(job: RepairJob, evidence: dict) -> str:
    """生成独立诊断提示；明确禁止把结果当成第三修复轮回写基线。"""
    return (
        "这是 Stage 3 的独立诊断复测，不是 78/90 基线的第三修复轮，结果不合并回基线。\n"
        "请阅读 SPEC.md、test_target.py 和当前 solution.py，结合下方脱敏失败证据诊断并完善实现。\n"
        "你只能修改 solution.py；不得修改规格、公开测试、评分器或其他文件。\n"
        "证据只给出失败类别与公开执行错误，不包含隐藏用例答案；请从规格推导通用实现。\n"
        f"trial={job.mode}/{job.task}/{job.trial}\n"
        f"evidence={json.dumps(evidence, ensure_ascii=False)}\n"
        "完成后必须精确运行 `uv run python -m unittest -q test_target.py`，"
        "不得改用其他命令或增删参数；测试通过后结束。"
    )


def _request_budget_misclassified(row: dict) -> bool:
    """识别被错误归入 provider_error 的本地请求预算耗尽。"""
    if row.get("leader_status") != "provider_error":
        return False
    for trace_value in row.get("traces", []):
        events, _issues = read_trace(Path(trace_value).resolve())
        for event in events:
            if event.get("event") != "provider_error":
                continue
            error = str(event.get("payload", {}).get("error", "")).lower()
            if any(marker in error for marker in _REQUEST_BUDGET_MARKERS):
                return True
    return False


def summarize_diagnostic_results(rows: list[dict]) -> dict[str, object]:
    """量化 12 项一次性结果，并只保留可公开字段，不复制路径或原始 Trace。"""
    expected = {item[:3] for item in FROZEN_STAGE3_FAILURES}
    identities = [trial_identity(row) for row in rows]
    if len(rows) != 12 or len(set(identities)) != 12 or set(identities) != expected:
        raise ValueError("诊断结果必须恰好覆盖 12 个冻结身份一次")
    statuses = Counter(str(row.get("leader_status", "unknown")) for row in rows)
    original_statuses = Counter(item[3] for item in FROZEN_STAGE3_FAILURES)
    deadline_or_loop_after = statuses["deadline"] + statuses["loop_detected"]
    successes = sum(row.get("passed") is True for row in rows)
    misclassified = sum(_request_budget_misclassified(row) for row in rows)
    known_tokens = sum((row.get("usage") or {}).get("total_tokens", 0) for row in rows)
    return {
        "trial_count": len(rows),
        "strict_successes": successes,
        "strict_failures": len(rows) - successes,
        "strict_success_rate": round(successes / len(rows), 6),
        "original_status_distribution": dict(sorted(original_statuses.items())),
        "status_distribution": dict(sorted(statuses.items())),
        "deadline_or_loop_before": original_statuses["deadline"]
        + original_statuses["loop_detected"],
        "deadline_or_loop_after": deadline_or_loop_after,
        "deadline_or_loop_target_met": deadline_or_loop_after <= 2,
        "request_budget_provider_misclassifications": misclassified,
        "provider_classification_target_met": misclassified == 0,
        "known_total_tokens": known_tokens,
        "unknown_usage_trials": sum(bool(row.get("usage_may_be_incomplete")) for row in rows),
        "total_duration_seconds": round(
            sum(float(row.get("duration_seconds", 0)) for row in rows), 3
        ),
        "results": [
            {
                "mode": row["mode"],
                "task": row["task"],
                "trial": row["trial"],
                "passed": row.get("passed") is True,
                "leader_status": row.get("leader_status"),
                "passed_checks": row.get("passed_checks"),
                "total_checks": row.get("total_checks"),
                "duration_seconds": row.get("duration_seconds"),
                "known_tokens": (row.get("usage") or {}).get("total_tokens"),
                "usage_may_be_incomplete": bool(row.get("usage_may_be_incomplete")),
                "failure_layers": sorted(
                    {
                        item.get("layer", "unknown")
                        for item in row.get("failure_checks", [])
                        if isinstance(item, dict) and item.get("passed") is not True
                    }
                ),
            }
            for row in rows
        ],
    }
