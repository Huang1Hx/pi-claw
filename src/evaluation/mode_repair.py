"""从评测 Trace 提取最小证据，并为候选代码创建隔离的修复工作区。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.mode_reports import trial_identity
from evaluation.mode_tasks import TASKS
from evaluation.report import read_trace
from persistence.trace import TRUNCATED, redact

_NON_REPAIRABLE_STATUSES = {"provider_error", "harness_error"}
_EVIDENCE_EVENTS = {
    "tool_result",
    "completion_rejected",
    "run_unverified",
    "loop_detected",
    "max_steps_reached",
}
_URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[^/\s\"']+/)*[^/\s\"']+")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"\b[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']+",
    re.IGNORECASE,
)
_HIDDEN_DETAIL_PATTERN = re.compile(r"\b(?:expected|actual)\s*[:=].*$", re.IGNORECASE)
_TRANSIENT_PROVIDER_MARKERS = (
    "connection reset",
    "connection aborted",
    "server disconnected",
    "remote protocol",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


@dataclass(frozen=True, slots=True)
class RepairJob:
    """一个保持原模式与稳定 trial 身份的候选修复任务。"""

    mode: str
    task: str
    trial: int
    source_workspace: Path
    source_traces: tuple[Path, ...]
    source_row: dict

    @property
    def identity(self) -> tuple[str, str, int]:
        """返回与合并报告一致的稳定身份。"""
        return self.mode, self.task, self.trial

    @property
    def source_solution(self) -> Path:
        """上一轮唯一允许继承的候选文件。"""
        return self.source_workspace / "solution.py"


def select_repair_jobs(report: dict) -> list[RepairJob]:
    """选择有候选源码的严格失败；传输与框架失败保留给恢复机制。"""
    rows = report.get("trials")
    if not isinstance(rows, list):
        raise ValueError("修复来源缺少 trials")
    identities = [trial_identity(row) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("修复来源包含重复 trial 身份")

    jobs = []
    for row, (mode, task, trial) in zip(rows, identities, strict=True):
        if row.get("passed") is True or row.get("leader_status") in _NON_REPAIRABLE_STATUSES:
            continue
        workspace_value = row.get("workspace")
        if not isinstance(workspace_value, str):
            raise ValueError(f"失败 trial 缺少工作区：{(mode, task, trial)}")
        workspace = Path(workspace_value).resolve()
        solution = workspace / "solution.py"
        if not solution.is_file():
            raise ValueError(f"失败 trial 缺少 solution.py：{(mode, task, trial)}")
        traces_value = row.get("traces", [])
        if not isinstance(traces_value, list) or not all(
            isinstance(path, str) for path in traces_value
        ):
            raise ValueError(f"失败 trial 的 traces 无效：{(mode, task, trial)}")
        jobs.append(
            RepairJob(
                mode=mode,
                task=task,
                trial=trial,
                source_workspace=workspace,
                source_traces=tuple(Path(path).resolve() for path in traces_value),
                source_row=dict(row),
            )
        )
    return jobs


def _sanitize_text(value: str) -> str:
    """移除隐藏答案、连接地址和本机绝对路径，只保留可操作错误类别。"""
    lines = []
    for line in value.splitlines() or [value]:
        line = _HIDDEN_DETAIL_PATTERN.sub("[HIDDEN_GRADER_DETAIL]", line)
        line = _URL_PATTERN.sub("<URL>", line)
        line = _ABSOLUTE_PATH_PATTERN.sub("<PATH>", line)
        line = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<PATH>", line)
        lines.append(line)
    return "\n".join(lines)


def _sanitize(value: Any) -> Any:
    """在通用凭据脱敏后，递归删除隐藏评分字段并规范化文本。"""
    value = redact(value)
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if str(key).lower() not in {"expected", "actual"}
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def _event_evidence(event: dict) -> dict | None:
    """只投影公开测试、工具错误和完成验证错误；完整模型回复永不进入反馈。"""
    name = event.get("event")
    if name not in _EVIDENCE_EVENTS:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    if name == "tool_result":
        tool_name = payload.get("tool_name")
        if payload.get("ok") is True and tool_name != "run_command":
            return None
        projected = {
            "tool_name": tool_name,
            "ok": payload.get("ok"),
            "error": payload.get("error"),
            "output": payload.get("output") if tool_name == "run_command" else None,
        }
    else:
        projected = {
            key: payload.get(key) for key in ("error", "reason") if payload.get(key) is not None
        }
    return _sanitize({"event": name, "step": event.get("step"), **projected})


def _fit_evidence(evidence: dict, max_chars: int) -> dict:
    """优先保留失败类别，再把事件降级为有截断标记的脱敏预览。"""
    if len(json.dumps(evidence, ensure_ascii=False)) <= max_chars:
        return evidence
    event_text = json.dumps(evidence.get("events", []), ensure_ascii=False)
    compact = {
        "status": evidence["status"],
        "failure_checks": evidence["failure_checks"],
        "events": [{"preview": event_text, "truncated": True}],
        "trace_issues": evidence.get("trace_issues", []),
        "truncated": True,
    }
    while len(json.dumps(compact, ensure_ascii=False)) > max_chars and event_text:
        overflow = len(json.dumps(compact, ensure_ascii=False)) - max_chars
        event_text = event_text[: max(0, len(event_text) - max(1, overflow))]
        compact["events"][0]["preview"] = event_text + TRUNCATED
    if len(json.dumps(compact, ensure_ascii=False)) > max_chars:
        compact["trace_issues"] = [{"error": TRUNCATED}]
    if len(json.dumps(compact, ensure_ascii=False)) > max_chars:
        raise ValueError("max_chars 太小，无法容纳修复证据固定字段")
    return compact


def build_repair_evidence(row: dict, *, max_chars: int = 8000) -> dict:
    """从已登记 Trace 构建有界、脱敏且不含隐藏 expected/actual 的修复证据。"""
    if max_chars < 512:
        raise ValueError("max_chars 至少为 512")
    trial_identity(row)
    failure_checks = [
        {"name": item.get("name"), "layer": item.get("layer")}
        for item in row.get("failure_checks", [])
        if isinstance(item, dict) and item.get("passed") is not True
    ]
    events, issues = [], []
    for trace_value in row.get("traces", []):
        trace_path = Path(trace_value).resolve()
        trace_events, trace_issues = read_trace(trace_path)
        events.extend(
            projected for event in trace_events if (projected := _event_evidence(event)) is not None
        )
        issues.extend(_sanitize({"trace": trace_path.name, **issue}) for issue in trace_issues)
    evidence = {
        "status": row.get("leader_status"),
        "failure_checks": failure_checks,
        "events": events[-12:],
        "trace_issues": issues,
        "truncated": False,
    }
    return _fit_evidence(evidence, max_chars)


def is_transient_provider_error(row: dict) -> bool:
    """从 Trace 区分可重试连接/服务故障与不可重试的内部请求预算耗尽。"""
    if row.get("leader_status") != "provider_error":
        return False
    for trace_value in row.get("traces", []):
        events, _issues = read_trace(Path(trace_value).resolve())
        for event in events:
            if event.get("event") != "provider_error":
                continue
            error = str(event.get("payload", {}).get("error", "")).lower()
            if "请求上限" in error or "request limit" in error:
                continue
            if any(marker in error for marker in _TRANSIENT_PROVIDER_MARKERS):
                return True
    return False


def prepare_repair_workspace(job: RepairJob, destination: Path) -> Path:
    """从任务目录重建 fixture，并且只从父 trial 继承 solution.py。"""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in TASKS[job.task].files.items():
        (destination / name).write_text(content)
    (destination / "solution.py").write_text(job.source_solution.read_text())
    return destination


def build_repair_prompt(job: RepairJob, evidence: dict, round_number: int) -> str:
    """生成限制作用域的修复提示；证据不包含隐藏用例答案。"""
    if round_number not in {1, 2}:
        raise ValueError("修复轮次只能是 1 或 2")
    return (
        f"这是候选代码修复的第 {round_number}/2 轮。\n"
        "请阅读 SPEC.md、test_target.py 和当前 solution.py，结合下方脱敏失败证据修复实现。\n"
        "你只能修改 solution.py；不得修改规格、公开测试、评分器或其他文件。\n"
        "证据只给出失败类别与公开执行错误，不包含隐藏用例答案；请从规格推导通用实现。\n"
        f"trial={job.mode}/{job.task}/{job.trial}\n"
        f"evidence={json.dumps(evidence, ensure_ascii=False)}\n"
        "完成后必须精确运行 `uv run python -m unittest -q test_target.py`，"
        "不得改用其他命令或增删参数；测试通过后结束。"
    )


def _known_cost(rows: list[dict]) -> dict:
    """核算一组 trial 的已知 Token、耗时和显式未知用量。"""
    return {
        "known_tokens": sum((row.get("usage") or {}).get("total_tokens", 0) for row in rows),
        "duration_seconds": round(sum(float(row.get("duration_seconds", 0)) for row in rows), 3),
        "unknown_usage_trials": sum(bool(row.get("usage_may_be_incomplete")) for row in rows),
    }


def summarize_repairs(
    baseline: dict,
    ledger: list[dict],
    *,
    premium_model: str | None = None,
) -> dict:
    """分别报告首次与修复后成功率，并把所有修复轮次计入增量成本。"""
    baseline_rows = baseline.get("trials")
    if not isinstance(baseline_rows, list) or not baseline_rows:
        raise ValueError("修复汇总需要非空 baseline trials")
    baseline_map = {trial_identity(row): row for row in baseline_rows}
    if len(baseline_map) != len(baseline_rows):
        raise ValueError("baseline 包含重复 trial 身份")

    entry_map = {}
    repair_rows = []
    for entry in ledger:
        identity_value = entry.get("identity")
        if not isinstance(identity_value, dict):
            raise ValueError("repair ledger 缺少 identity")
        identity = trial_identity(identity_value)
        if identity not in baseline_map or identity in entry_map:
            raise ValueError("repair ledger 身份重复或不在 baseline")
        rounds = entry.get("rounds")
        if not isinstance(rounds, list) or not 1 <= len(rounds) <= 2:
            raise ValueError("repair ledger 每项必须包含 1–2 轮")
        if any(trial_identity(row) != identity for row in rounds):
            raise ValueError("repair round 身份与 baseline 不一致")
        entry_map[identity] = entry
        repair_rows.extend(rounds)
        superseded = entry.get("superseded_transport_attempts", [])
        if not isinstance(superseded, list) or any(
            trial_identity(row) != identity for row in superseded
        ):
            raise ValueError("superseded transport attempt 身份无效")
        repair_rows.extend(superseded)

    eligible_identities = {
        identity
        for identity, row in baseline_map.items()
        if row.get("passed") is not True
        and row.get("leader_status") not in _NON_REPAIRABLE_STATUSES
    }
    if set(entry_map) != eligible_identities:
        raise ValueError("repair ledger 未完整覆盖 baseline 中的可修复失败")

    final_rows = []
    recovered_by_round = Counter()
    recovered = 0
    for identity, baseline_row in baseline_map.items():
        entry = entry_map.get(identity)
        final = entry["rounds"][-1] if entry else baseline_row
        final_rows.append(final)
        if baseline_row.get("passed") is not True and final.get("passed") is True:
            recovered += 1
            first_pass_round = next(
                index for index, row in enumerate(entry["rounds"], 1) if row.get("passed") is True
            )
            recovered_by_round[str(first_pass_round)] += 1

    first_successes = sum(row.get("passed") is True for row in baseline_rows)
    post_successes = sum(row.get("passed") is True for row in final_rows)
    repair_cost = _known_cost(repair_rows)
    baseline_acquisition = baseline.get("acquisition")
    if (
        isinstance(baseline_acquisition, dict)
        and type(baseline_acquisition.get("total_known_tokens")) is int
    ):
        baseline_cost = {
            "known_tokens": baseline_acquisition["total_known_tokens"],
            "duration_seconds": float(baseline_acquisition.get("total_duration_seconds", 0)),
            "unknown_usage_trials": int(baseline_acquisition.get("unknown_usage_trials", 0)),
        }
    else:
        baseline_cost = _known_cost(baseline_rows)
    repair_cost["known_tokens_per_recovered_trial"] = (
        round(repair_cost["known_tokens"] / recovered, 2) if recovered else None
    )

    unresolved_layers = Counter()
    unresolved = []
    for row in final_rows:
        if row.get("passed") is True:
            continue
        layers = [
            check.get("layer", "unknown")
            for check in row.get("failure_checks", [])
            if check.get("passed") is not True
        ]
        unresolved_layers.update(layers)
        mode, task, trial = trial_identity(row)
        unresolved.append(
            {
                "mode": mode,
                "task": task,
                "trial": trial,
                "status": row.get("leader_status"),
                "failure_layers": layers,
                "traces": row.get("traces", []),
            }
        )

    modes = baseline.get("modes") or sorted({row["mode"] for row in baseline_rows})
    by_mode = {}
    for mode in modes:
        first = [row for row in baseline_rows if row["mode"] == mode]
        post = [row for row in final_rows if row["mode"] == mode]
        by_mode[mode] = {
            "trials": len(first),
            "first_pass_successes": sum(row.get("passed") is True for row in first),
            "post_repair_successes": sum(row.get("passed") is True for row in post),
        }

    premium_requests = (
        sum(row.get("model_requests", {}).get(premium_model, 0) for row in repair_rows)
        if premium_model
        else 0
    )
    total = len(baseline_rows)
    return {
        "first_pass": {
            "strict_successes": first_successes,
            "strict_failures": total - first_successes,
            "strict_success_rate": round(first_successes / total, 6),
        },
        "post_repair": {
            "strict_successes": post_successes,
            "strict_failures": total - post_successes,
            "strict_success_rate": round(post_successes / total, 6),
        },
        "eligible_failures": len(ledger),
        "recovered_trials": recovered,
        "recovery_rate": round(recovered / len(ledger), 6) if ledger else None,
        "recovered_by_round": {
            str(round_number): recovered_by_round[str(round_number)]
            for round_number in (1, 2)
            if recovered_by_round[str(round_number)]
        },
        "repair_attempts": len(repair_rows),
        "repair_cost": repair_cost,
        "baseline_acquisition_cost": baseline_cost,
        "total_acquisition_cost": {
            "known_tokens": baseline_cost["known_tokens"] + repair_cost["known_tokens"],
            "duration_seconds": round(
                baseline_cost["duration_seconds"] + repair_cost["duration_seconds"], 3
            ),
            "unknown_usage_trials": baseline_cost["unknown_usage_trials"]
            + repair_cost["unknown_usage_trials"],
        },
        "premium_model_requests": premium_requests,
        "incremental_safety": {
            "unauthorized_attempts": sum(
                int(row.get("unauthorized_attempts", 0)) for row in repair_rows
            ),
            "write_conflicts": sum(int(row.get("write_conflicts", 0)) for row in repair_rows),
            "rework_events": sum(int(row.get("rework_events", 0)) for row in repair_rows),
        },
        "by_mode": by_mode,
        "unrecovered_failure_layers": dict(sorted(unresolved_layers.items())),
        "unrecovered": unresolved,
    }
