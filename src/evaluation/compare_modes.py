"""用同一任务、评分器和预算比较单 Agent、顺序工作流与主从多 Agent。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from math import ceil
from pathlib import Path
from statistics import median

from config import load_settings
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.context import ContextManager
from core.prompts import build_system_prompt
from core.team import ReadOnlySubagentTeam, SubagentAssignment
from evaluation.mode_grading import GRADER_VERSION, calibrate, grade, safe_functions
from evaluation.mode_tasks import TASKS
from evaluation.report import read_trace
from persistence.trace import TraceWriter, redact
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from security.workspace import Workspace
from tools import ToolExecutor

MODES = ("single", "workflow", "team")
VERIFY_ARGV = ["uv", "run", "python", "-m", "unittest", "-q", "test_target.py"]
PUBLIC_SUMMARY_FIELDS = (
    "schema_version",
    "grader_version",
    "generated_at",
    "real_model",
    "network_used",
    "source_sha256",
    "modes",
    "tasks",
    "trials_per_task",
    "workers",
    "deadline_seconds",
    "max_steps",
    "transport",
    "models",
    "premium_model",
    "accounting",
    "continuation",
    "by_mode",
    "by_mode_and_task",
)
_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()


def save_json(path: Path, data) -> None:
    """所有评测产物统一脱敏；Settings、Key 与 URL 不进入报告。"""
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


def build_public_summary(report: dict) -> dict:
    """用字段白名单生成可提交摘要；逐 trial 路径、Trace 和连接信息一律不复制。"""
    return {name: report[name] for name in PUBLIC_SUMMARY_FIELDS if name in report}


def _trial_identity(row: dict) -> tuple[str, str, int]:
    """用稳定三元组关联原 trial 与恢复结果，避免依赖目录遍历顺序。"""
    mode, task, trial = row.get("mode"), row.get("task"), row.get("trial")
    if mode not in MODES or task not in TASKS or type(trial) is not int or not 1 <= trial <= 5:
        raise ValueError("无效的 trial 身份")
    return mode, task, trial


def select_resume_jobs(report: dict) -> list[tuple[str, str, int]]:
    """只选择模型传输失败项；语义失败、循环和未验证结果不得通过续跑洗掉。"""
    rows = report.get("trials")
    if not isinstance(rows, list):
        raise ValueError("续跑来源缺少 trials")
    identities = [_trial_identity(row) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("续跑来源包含重复 trial 身份")
    return [
        identity
        for row, identity in zip(rows, identities, strict=True)
        if row.get("leader_status") == "provider_error"
    ]


def combine_resumed_reports(source_report: dict, replacements: list[dict]) -> list[dict]:
    """替换传输失败项，并用当前评分器离线重评其余 trial；原目录和 Trace 保持不变。"""
    source_rows = source_report.get("trials")
    if not isinstance(source_rows, list):
        raise ValueError("续跑来源缺少 trials")
    expected = set(select_resume_jobs(source_report))
    replacement_map = {_trial_identity(row): row for row in replacements}
    if len(replacement_map) != len(replacements) or set(replacement_map) != expected:
        raise ValueError("恢复结果与 provider_error trial 清单不一致")

    combined = []
    for row in source_rows:
        identity = _trial_identity(row)
        if identity in replacement_map:
            combined.append(replacement_map[identity])
            continue
        workspace = Path(row["workspace"])
        folder = workspace.parent
        if not workspace.is_dir() or not (folder / "run-result.json").is_file():
            raise ValueError(f"无法离线重评原 trial：{identity}")
        combined.append(
            build_trial_report(
                folder,
                {
                    "mode": row["mode"],
                    "task": row["task"],
                    "trial": row["trial"],
                    "duration_seconds": row["duration_seconds"],
                },
            )
        )
    return combined


def approval_for(task_id: str, workspace: Path):
    """唯一写者只可修改 solution.py，并仅可执行固定公开测试。"""
    task = TASKS[task_id]

    def approve(call, decision, preview=None):
        """Policy 的 DENY 不会进入回调；这里进一步收窄允许副作用。"""
        args = call.arguments
        if call.name in {"write_or_replace", "apply_patch"}:
            return args.get("path") in {"solution.py", "./solution.py"}
        if call.name != "run_command" or args.get("argv") != VERIFY_ARGV:
            return False
        try:
            boundary = Workspace(workspace)
            if boundary.resolve(args.get("cwd", ".")) != boundary.root:
                return False
            if (workspace / "SPEC.md").read_text() != task.spec:
                return False
            if (workspace / "test_target.py").read_text() != task.public_test:
                return False
            safe_functions((workspace / "solution.py").read_text(), task.function_name)
        except (SyntaxError, TypeError, ValueError) as error:
            return f"候选代码未通过受限语法检查：{error}"
        except OSError as error:
            return f"评测文件读取失败：{error}"
        return True

    return approve


def _provider(settings, profile: str, args) -> RoutedProvider:
    """每个 Agent 使用独立 Provider 状态；相同连接仅共享配置值。"""
    return RoutedProvider(
        settings,
        mode=profile,
        max_attempts=args.max_attempts,
        retry_base_delay=args.retry_base_delay,
        max_retry_delay=args.max_retry_delay,
    )


def _executor(workspace: Path, task_id: str, *, writable: bool) -> ToolExecutor:
    """主 Agent 可进入精确审批；子 Agent 从策略层保持只读且禁止命令。"""
    return ToolExecutor(
        WorkspacePolicy(
            workspace,
            allow_write=writable,
            allow_commands=writable,
            command_profile="coding" if writable else "minimal",
        ),
        approval_callback=approval_for(task_id, workspace) if writable else None,
        command_timeout=10,
        max_write_bytes=12_000,
    )


def _agent(provider, executor, trace: Path, workspace: Path, *, steps: int) -> Agent:
    """所有模式复用相同 Agent Loop、上下文与完成证据检查。"""
    return Agent(
        provider,
        executor,
        max_steps=steps,
        trace=TraceWriter(trace),
        system_prompt=build_system_prompt(workspace),
        context_manager=ContextManager(),
        completion_verifier=ToolEvidenceCompletionVerifier(),
        streaming=False,
    )


def worker(args) -> None:
    """在 trial 子进程内运行一种架构；每个 Agent 都有独立 Provider、上下文和 Trace。"""
    folder = args.output.resolve()
    workspace = folder / "workspace"
    task = TASKS[args.worker]
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    # 对照关注架构，不额外调用模型做意图分类；工作流仍使用当前角色和返工上限。
    workflow_settings = replace(
        settings,
        routing=replace(settings.routing, intent_strategy="rules"),
    )
    profile_map = {
        "single": [settings.workflow.coder],
        "workflow": [
            settings.workflow.planner,
            settings.workflow.coder,
            settings.workflow.reviewer,
        ],
        "team": [
            settings.workflow.coder,
            settings.workflow.planner,
            settings.workflow.reviewer,
        ],
    }
    save_json(
        folder / "config-snapshot.json",
        {
            "mode": args.mode,
            "profiles": [
                {
                    "profile": name,
                    "model": settings.profiles[name].model,
                    "thinking_mode": settings.profiles[name].thinking_mode,
                    "thinking_format": settings.profiles[name].thinking_format,
                }
                for name in profile_map[args.mode]
            ],
            "max_steps": args.max_steps,
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
            "command_timeout_seconds": 10,
            "workflow": {
                "max_planning_requests": settings.workflow.max_planning_requests,
                "max_review_requests": settings.workflow.max_review_requests,
                "max_review_rounds": settings.workflow.max_review_rounds,
            }
            if args.mode == "workflow"
            else None,
        },
    )
    started = time.monotonic()
    writable = _executor(workspace, args.worker, writable=True)
    if args.mode == "single":
        result = _agent(
            _provider(settings, settings.workflow.coder, args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        ).run(task.prompt)
        payload = {
            "mode": args.mode,
            "leader": result.to_dict(),
            "subagents": [],
            "usage": result.usage,
            "duration_seconds": round(time.monotonic() - started, 3),
            "max_parallel_subagents": 0,
        }
    elif args.mode == "workflow":
        result = _agent(
            _provider(workflow_settings, "auto", args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        ).run(task.prompt)
        payload = {
            "mode": args.mode,
            "leader": result.to_dict(),
            "subagents": [],
            "usage": result.usage,
            "duration_seconds": round(time.monotonic() - started, 3),
            "max_parallel_subagents": 0,
        }
    else:
        leader = _agent(
            _provider(settings, settings.workflow.coder, args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        )
        implementation = _agent(
            _provider(settings, settings.workflow.planner, args),
            _executor(workspace, args.worker, writable=False),
            folder / "trace-subagent-implementation.jsonl",
            workspace,
            steps=6,
        )
        risks = _agent(
            _provider(settings, settings.workflow.reviewer, args),
            _executor(workspace, args.worker, writable=False),
            folder / "trace-subagent-risks.jsonl",
            workspace,
            steps=6,
        )
        team = ReadOnlySubagentTeam(
            leader,
            {"implementation": implementation, "risks": risks},
            max_workers=2,
        )
        result = team.run(
            task.prompt,
            (
                SubagentAssignment(
                    "implementation",
                    "实现分析",
                    "只读检查 SPEC.md、solution.py 和 test_target.py。定位实现缺陷，"
                    "给主 Agent 一份具体修改建议；不得写文件或运行命令。",
                ),
                SubagentAssignment(
                    "risks",
                    "边界与测试分析",
                    "独立只读检查 SPEC.md、solution.py 和 test_target.py。推导公开测试之外的"
                    "高风险边界与回归点；不得写文件或运行命令。",
                ),
            ),
        )
        payload = {"mode": args.mode, **result.to_dict()}
    payload["duration_seconds"] = round(time.monotonic() - started, 3)
    save_json(folder / "run-result.json", payload)


def _events(folder: Path, mode: str) -> tuple[list[dict], list[dict]]:
    """读取所有参与者 Trace，给每条事件补 participant；损坏项不阻断其他 Trace。"""
    events, issues = [], []
    paths = sorted(folder.glob("trace-*.jsonl"))
    expected = _expected_participants(mode)
    found = {path.stem.removeprefix("trace-") for path in paths}
    for participant in sorted(expected - found):
        issues.append({"participant": participant, "line": None, "error": "trace missing"})
    for path in paths:
        participant = path.stem.removeprefix("trace-")
        rows, errors = read_trace(path)
        if not rows:
            issues.append({"participant": participant, "line": None, "error": "trace empty"})
        events.extend(
            {**row, "participant": participant, "trace": str(path.resolve())} for row in rows
        )
        issues.extend(
            {**error, "participant": participant, "trace": str(path.resolve())} for error in errors
        )
    return events, issues


def _expected_participants(mode: str) -> set[str]:
    """顺序工作流仍是一个 Agent；主从模式有一个 leader 和两个独立子 Agent。"""
    expected = {"leader"}
    if mode == "team":
        expected |= {"subagent-implementation", "subagent-risks"}
    return expected


def _partial_usage(events: list[dict], mode: str) -> dict:
    """worker 被期限/中断终止时，从每个 Trace 最后一次累计值恢复已知 Token。"""
    latest = {}
    for event in events:
        if event["event"] == "usage_updated" and isinstance(event["payload"], dict):
            latest[event["participant"]] = event["payload"]
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "requests",
        "requests_with_usage",
        "requests_without_usage",
        "retries",
    )
    total = {key: sum(int(row.get(key, 0)) for row in latest.values()) for key in keys}
    # 没有完整 run-result 就无法确认被终止瞬间的在途请求；未知量绝不按已知零处理。
    total["unknown_participants"] = len(_expected_participants(mode))
    return total


def _attempted_requests(result: dict, usage: dict) -> int:
    """汇总主从路由已发起的逻辑请求；超时请求没有 ModelReply，但仍属于总消耗缺口。"""
    participants = [result.get("leader", {})]
    participants.extend(
        item.get("result", {}) for item in result.get("subagents", []) if isinstance(item, dict)
    )
    attempted = 0
    for participant in participants:
        routing = participant.get("routing") if isinstance(participant, dict) else None
        participant_usage = participant.get("usage") if isinstance(participant, dict) else None
        if isinstance(routing, dict) and type(routing.get("requests")) is int:
            attempted += routing["requests"]
        elif isinstance(participant_usage, dict):
            attempted += int(participant_usage.get("requests", 0))
    return max(attempted, int(usage.get("requests", 0)))


def _model_usage(result: dict, events: list[dict]) -> dict[str, dict[str, int]]:
    """按具体模型核算已知 Token，并把已路由但无 usage 的请求保留为缺口。"""
    attempted = Counter()
    profile_models = {}
    for event in events:
        if event["event"] != "model_routed":
            continue
        profile = event["payload"].get("profile")
        model = event["payload"].get("model")
        if isinstance(profile, str) and isinstance(model, str):
            profile_models[(event["participant"], profile)] = model
            attempted[model] += 1

    participants = [("leader", result.get("leader", {}))]
    participants.extend(
        (f"subagent-{item.get('name')}", item.get("result", {}))
        for item in result.get("subagents", [])
        if isinstance(item, dict)
    )
    known: dict[str, Counter] = {}
    for participant_name, participant in participants:
        routing = participant.get("routing") if isinstance(participant, dict) else None
        models = routing.get("models") if isinstance(routing, dict) else None
        if not isinstance(models, dict):
            continue
        for profile, usage in models.items():
            if not isinstance(usage, dict):
                continue
            model = profile_models.get((participant_name, profile))
            if model is None:
                candidates = {
                    value
                    for (event_participant, event_profile), value in profile_models.items()
                    if event_profile == profile
                }
                model = candidates.pop() if len(candidates) == 1 else f"profile:{profile}"
            totals = known.setdefault(model, Counter())
            totals["requests_with_usage"] += int(usage.get("requests", 0))
            totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
            totals["completion_tokens"] += int(usage.get("completion_tokens", 0))

    rows = {}
    for model in sorted(set(attempted) | set(known)):
        values = known.get(model, Counter())
        with_usage = values["requests_with_usage"]
        attempted_requests = max(attempted[model], with_usage)
        rows[model] = {
            "attempted_requests": attempted_requests,
            "requests_with_usage": with_usage,
            "requests_without_usage": max(0, attempted_requests - with_usage),
            "prompt_tokens": values["prompt_tokens"],
            "completion_tokens": values["completion_tokens"],
            "total_tokens": values["prompt_tokens"] + values["completion_tokens"],
        }
    return rows


def _reconcile_usage(result: dict, usage: dict) -> dict:
    """把已知 Token 与无 usage 的已发起请求分开；未知请求绝不虚构 Token。"""
    reconciled = dict(usage)
    attempted = _attempted_requests(result, reconciled)
    with_usage = int(reconciled.get("requests_with_usage", 0))
    reconciled["attempted_requests"] = attempted
    reconciled["requests"] = attempted
    reconciled["requests_without_usage"] = max(
        int(reconciled.get("requests_without_usage", 0)), attempted - with_usage
    )
    return reconciled


def _public_test_passed(events: list[dict]) -> bool:
    """严格成功要求唯一写者真实执行过固定公开测试且返回成功。"""
    return any(
        event["participant"] == "leader"
        and event["event"] == "tool_result"
        and event["payload"].get("tool_name") == "run_command"
        and event["payload"].get("ok") is True
        and event["payload"].get("output", {}).get("argv") == VERIFY_ARGV
        for event in events
    )


def build_trial_report(folder: Path, metadata: dict) -> dict:
    """统一评分三种模式，并从所有参与者 Trace 统计越权、冲突和返工。"""
    result_path = folder / "run-result.json"
    try:
        result = json.loads(result_path.read_text())
        leader = result["leader"]
        if not isinstance(leader.get("status"), str):
            raise ValueError("invalid leader status")
    except (OSError, ValueError, KeyError, TypeError):
        result = {"leader": {"status": "harness_error"}, "usage": None, "subagents": []}
        leader = result["leader"]
    events, trace_issues = _events(folder, metadata["mode"])
    checks = grade(metadata["task"], folder / "workspace", leader["status"])
    checks.insert(
        2,
        {
            "name": "public_test_passed",
            "passed": _public_test_passed(events),
            "layer": "L0_execution",
            "detail": "必须由 leader 成功执行固定 unittest argv",
        },
    )
    checks.insert(
        1,
        {
            "name": "trace_integrity",
            "passed": not trace_issues,
            "layer": "L0_execution",
            "detail": json.dumps(trace_issues, ensure_ascii=False) if trace_issues else "",
        },
    )
    tool_results = [event for event in events if event["event"] == "tool_result"]
    unauthorized = {}
    for event in tool_results:
        payload = event["payload"]
        if payload.get("metadata", {}).get("policy", {}).get("action") == "deny" or (
            payload.get("error") or ""
        ).startswith("未获批准："):
            unauthorized[(event["participant"], payload.get("call_id"))] = event
    # 子 Agent 的副作用提议即使只在 model_reply 中出现，也属于越权尝试。
    for event in events:
        if not event["participant"].startswith("subagent-") or event["event"] != "model_reply":
            continue
        calls = event["payload"].get("tool_calls") or []
        if event["payload"].get("tool_call"):
            calls = [event["payload"]["tool_call"]]
        for call in calls:
            if call.get("name") in {"write_or_replace", "apply_patch", "run_command"}:
                unauthorized.setdefault((event["participant"], call.get("id")), event)
    # 顺序工作流会在 Provider 边界拒绝只读阶段的副作用，此时不会生成 ToolResult。
    for event in events:
        if event["event"] == "run_unverified" and "非只读工具" in (
            event["payload"].get("error") or ""
        ):
            unauthorized.setdefault((event["participant"], event["step"]), event)
    conflicts = [
        event
        for event in tool_results
        if "目标文件发生变化" in (event["payload"].get("error") or "")
    ]
    rework = sum(event["event"] == "completion_rejected" for event in events)
    rework += sum(
        event["event"] == "workflow_artifact"
        and event["payload"].get("kind") == "review"
        and event["payload"].get("data", {}).get("verdict") == "changes_requested"
        for event in events
    )
    passed = all(item["passed"] is True for item in checks)
    usage = result.get("usage")
    usage_source = "run_result"
    if usage is None:
        usage = _partial_usage(events, metadata["mode"])
        usage_source = "partial_trace"
    else:
        usage = _reconcile_usage(result, usage)
    model_usage = _model_usage(result, events)
    models = sorted(model_usage)
    model_requests = {model: values["attempted_requests"] for model, values in model_usage.items()}
    return {
        **metadata,
        "grader_version": GRADER_VERSION,
        "passed": passed,
        "leader_status": leader["status"],
        "checks": checks,
        "passed_checks": sum(item["passed"] is True for item in checks),
        "total_checks": len(checks),
        "usage": usage,
        "usage_source": usage_source,
        "usage_may_be_incomplete": usage_source == "partial_trace"
        or bool(usage.get("unknown_participants"))
        or bool(usage.get("requests_without_usage")),
        "duration_seconds": result.get("duration_seconds", metadata["duration_seconds"]),
        "models_used": models,
        "model_requests": model_requests,
        "model_usage": model_usage,
        "participants": len(_expected_participants(metadata["mode"])),
        "max_parallel_subagents": result.get("max_parallel_subagents", 0),
        "tool_calls": len(tool_results),
        "unauthorized_attempts": len(unauthorized),
        "write_conflicts": len(conflicts),
        "rework_events": rework,
        "trace_issues": trace_issues,
        "workspace": str((folder / "workspace").resolve()),
        "traces": [str(path.resolve()) for path in sorted(folder.glob("trace-*.jsonl"))],
        "failure_checks": [item for item in checks if item["passed"] is not True],
    }


def reliability(rows: list[dict]) -> dict:
    """按任务分组计算实测 pass@k/pass^k；不同模式绝不混成重复试验。"""
    grouped = {}
    for row in rows:
        grouped.setdefault(row["task"], []).append(row["passed"])
    counts = {len(values) for values in grouped.values()}
    if not grouped or len(counts) != 1:
        raise ValueError("每个任务必须有相同且非零的重复次数")
    k = counts.pop()
    return {
        "tasks": len(grouped),
        "trials": len(rows),
        "k": k,
        "strict_success_rate": sum(row["passed"] for row in rows) / len(rows),
        f"observed_pass_at_{k}": sum(any(values) for values in grouped.values()) / len(grouped),
        f"observed_pass_power_{k}": sum(all(values) for values in grouped.values()) / len(grouped),
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    """计算不插值的最近秩分位数；P95 会保留真实观测到的长尾值。"""
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def summarize(rows: list[dict], *, premium_model: str | None = None) -> dict:
    """汇总成功、长尾耗时、模型总请求、Token、安全边界和返工。"""
    successes = sum(row["passed"] for row in rows)
    known_tokens = sum((row["usage"] or {}).get("total_tokens", 0) for row in rows)
    durations = [row["duration_seconds"] for row in rows]
    statuses = Counter(row.get("leader_status", "unknown") for row in rows)
    failure_layers = Counter(
        item.get("layer", "unknown")
        for row in rows
        for item in row.get("failure_checks", [])
        if item.get("passed") is not True
    )
    model_requests = Counter()
    model_usage: dict[str, Counter] = {}
    for row in rows:
        model_requests.update(row.get("model_requests", {}))
        for model, usage in row.get("model_usage", {}).items():
            totals = model_usage.setdefault(model, Counter())
            totals.update(usage)
    model_usage_distribution = {
        model: {
            name: values[name]
            for name in (
                "attempted_requests",
                "requests_with_usage",
                "requests_without_usage",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            )
        }
        for model, values in sorted(model_usage.items())
    }
    empty_model_usage = {
        "attempted_requests": 0,
        "requests_with_usage": 0,
        "requests_without_usage": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    timeout_trials = statuses["deadline"]
    return {
        "models_used": sorted({model for row in rows for model in row["models_used"]}),
        "model_request_distribution": dict(sorted(model_requests.items())),
        "premium_model_requests": model_requests[premium_model] if premium_model else 0,
        "model_usage_distribution": model_usage_distribution,
        "premium_model_usage": model_usage_distribution.get(premium_model, empty_model_usage)
        if premium_model
        else empty_model_usage,
        "reliability": reliability(rows),
        "strict_successes": successes,
        "strict_failures": len(rows) - successes,
        "p50_duration_seconds": round(median(durations), 3),
        "p95_duration_seconds": round(_nearest_rank(durations, 0.95), 3),
        "median_duration_seconds": round(median(durations), 3),
        "total_duration_seconds": round(sum(durations), 3),
        "timeout_trials": timeout_trials,
        "timeout_rate": round(timeout_trials / len(rows), 6),
        "status_distribution": dict(sorted(statuses.items())),
        "failure_layer_distribution": dict(sorted(failure_layers.items())),
        "known_total_tokens": known_tokens,
        "unattributed_known_tokens": max(
            0,
            known_tokens
            - sum(values["total_tokens"] for values in model_usage_distribution.values()),
        ),
        "unknown_usage_trials": sum(row["usage_may_be_incomplete"] for row in rows),
        "known_tokens_per_strict_success": round(known_tokens / successes, 2)
        if successes
        else None,
        "tokens_per_success_is_lower_bound": any(row["usage_may_be_incomplete"] for row in rows),
        "tool_calls": sum(row["tool_calls"] for row in rows),
        "unauthorized_attempts": sum(row["unauthorized_attempts"] for row in rows),
        "write_conflicts": sum(row["write_conflicts"] for row in rows),
        "rework_events": sum(row["rework_events"] for row in rows),
        "max_parallel_subagents": max(row["max_parallel_subagents"] for row in rows),
    }


def run_trial(mode: str, task_id: str, number: int, args) -> dict:
    """父进程提供 trial 墙钟期限；超时清理整个 worker 进程组并保留失败证据。"""
    folder = args.output / mode / f"{task_id}-{number}"
    folder.mkdir(parents=True)
    workspace = folder / "workspace"
    workspace.mkdir()
    for name, content in TASKS[task_id].files.items():
        (workspace / name).write_text(content)
    save_json(
        folder / "task.json",
        {"task": task_id, "title": TASKS[task_id].title, "prompt": TASKS[task_id].prompt},
    )
    print(f"[开始] mode={mode} task={task_id} trial={number}", flush=True)
    command = [
        sys.executable,
        "-m",
        "evaluation.compare_modes",
        "--worker",
        task_id,
        "--mode",
        mode,
        "--output",
        str(folder.resolve()),
        "--config",
        str(args.config.resolve()),
        "--max-steps",
        str(args.max_steps),
        "--max-attempts",
        str(args.max_attempts),
        "--retry-base-delay",
        str(args.retry_base_delay),
        "--max-retry-delay",
        str(args.max_retry_delay),
    ]
    started = time.monotonic()
    timed_out = False
    with (folder / "progress.log").open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=stream, start_new_session=True)
        with _PROCESS_LOCK:
            _ACTIVE_PROCESSES.add(process)
        try:
            try:
                process.wait(timeout=args.deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            with _PROCESS_LOCK:
                _ACTIVE_PROCESSES.discard(process)
    progress = folder / "progress.log"
    progress.write_text(redact(progress.read_text(errors="replace")))
    result_path = folder / "run-result.json"
    if not result_path.exists():
        save_json(
            result_path,
            {
                "mode": mode,
                "leader": {
                    "status": "deadline" if timed_out else "harness_error",
                    "process_returncode": process.returncode,
                },
                "usage": None,
                "subagents": [],
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )
    report = build_trial_report(
        folder,
        {
            "mode": mode,
            "task": task_id,
            "trial": number,
            "duration_seconds": round(time.monotonic() - started, 3),
        },
    )
    save_json(folder / "grade.json", report)
    print(
        f"[评分] mode={mode} task={task_id} trial={number}: "
        f"passed={report['passed']} checks={report['passed_checks']}/{report['total_checks']} "
        f"tokens={(report['usage'] or {}).get('total_tokens', 'unknown')} "
        f"duration={report['duration_seconds']}s",
        flush=True,
    )
    return report


def stop_active_trials() -> None:
    """批次被用户中断时终止所有独立进程组，避免模型请求成为孤儿进程。"""
    with _PROCESS_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)


def main() -> int:
    """默认只校准；--live 才运行真实模型，恢复批次写入新目录而不覆盖原证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("pi-claw.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="从已有 report.json 仅续跑 provider_error，并重新生成完整合并报告",
    )
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=420)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="每个普通模型逻辑请求的最大 HTTP 尝试次数；profile 可单独收紧",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=2.0,
        help="暂时性失败后的指数退避基数秒数",
    )
    parser.add_argument(
        "--max-retry-delay",
        type=float,
        default=10.0,
        help="单次重试等待上限秒数，也用于限制 Retry-After",
    )
    parser.add_argument("--task", choices=tuple(TASKS), action="append")
    parser.add_argument("--mode", choices=MODES, action="append")
    parser.add_argument("--worker", choices=tuple(TASKS), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 10:
        parser.error("max-attempts 为 1–10")
    if not 0 <= args.retry_base_delay <= args.max_retry_delay <= 60:
        parser.error("重试等待须满足 0 <= retry-base-delay <= max-retry-delay <= 60")
    if args.worker:
        args.mode = args.mode[0]
        worker(args)
        return 0
    if not 1 <= args.trials <= 5 or not 1 <= args.workers <= 3:
        parser.error("trials 为 1–5；workers 为 1–3")
    if not 30 <= args.deadline <= 900 or not 4 <= args.max_steps <= 40:
        parser.error("deadline 为 30–900 秒；max-steps 为 4–40")
    source_report = None
    resume_jobs = None
    if args.resume_from:
        if not args.live:
            parser.error("--resume-from 必须与 --live 一起使用")
        if args.mode or args.task:
            parser.error("续跑模式从来源报告恢复，不能同时指定 --mode 或 --task")
        resume_root = args.resume_from.resolve()
        try:
            source_report = json.loads((resume_root / "report.json").read_text())
            resume_jobs = select_resume_jobs(source_report)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法读取续跑来源：{error}")
        if not resume_jobs:
            parser.error("续跑来源没有 provider_error trial")
        modes = list(source_report["modes"])
        tasks = list(source_report["tasks"])
        args.trials = int(source_report["trials_per_task"])
        args.deadline = int(source_report["deadline_seconds"])
        args.max_steps = int(source_report["max_steps"])
    else:
        modes = list(dict.fromkeys(args.mode or MODES))
        tasks = list(dict.fromkeys(args.task or TASKS))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止真实模型请求")
    print(f"[校准] {len(TASKS)} 项参考实现通过，初始缺陷均被隐藏用例检出。", flush=True)
    if not args.live:
        print("未指定 --live：未调用模型。", flush=True)
        return 0
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        parser.error("比较需要已启用且模型完整的 planner/coder/reviewer 工作流配置")
    source_root = Path(__file__).resolve().parents[1]
    fingerprint = sha256(
        b"".join(
            path.relative_to(source_root).as_posix().encode() + path.read_bytes()
            for path in sorted(source_root.rglob("*.py"))
        )
    ).hexdigest()
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    if source_report and (
        source_report.get("models") != models or source_report.get("premium_model") != premium_model
    ):
        parser.error("续跑必须沿用来源报告中的 DeepSeek/Kimi 模型分配")
    manifest = {
        "schema_version": 3,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": fingerprint,
        "modes": modes,
        "tasks": tasks,
        "trials_per_task": args.trials,
        "workers": args.workers,
        "deadline_seconds": args.deadline,
        "max_steps": args.max_steps,
        "transport": {
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
        },
        "models": models,
        "premium_model": premium_model,
        "accounting": "Token 统计包含主 Agent、全部子 Agent、工作流阶段与返工；不计算货币费用",
    }
    if source_report:
        manifest["continuation"] = {
            "selected_status": "provider_error",
            "requested_trials": len(resume_jobs),
            "source_grader_version": source_report.get("grader_version"),
            "source_sha256": source_report.get("source_sha256"),
            "initial_workers": source_report.get("workers"),
            "continuation_workers": args.workers,
            "retained_trials_regraded": len(source_report["trials"]) - len(resume_jobs),
        }
    save_json(args.output / "manifest.json", manifest)
    jobs = resume_jobs or [
        (mode, task_id, number)
        for task_id in tasks
        for number in range(1, args.trials + 1)
        for mode in modes
    ]
    print(
        f"[{'续跑' if source_report else '批次'}] {len(jobs)} trials；modes={modes}；"
        f"tasks={tasks}；并发={args.workers}；"
        f"每 trial 期限={args.deadline}s；每请求最多 {args.max_attempts} 次尝试，"
        f"退避基数={args.retry_base_delay}s。",
        flush=True,
    )
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = [
        pool.submit(run_trial, mode, task_id, number, args) for mode, task_id, number in jobs
    ]
    try:
        reports = [future.result() for future in futures]
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        stop_active_trials()
        pool.shutdown(wait=True, cancel_futures=True)
        print("[中断] 已终止运行中的 trial 进程组；未生成汇总报告。", flush=True)
        return 130
    else:
        pool.shutdown()
    if source_report:
        reports = combine_resumed_reports(source_report, reports)
        manifest["continuation"]["remaining_provider_errors"] = sum(
            row["leader_status"] == "provider_error" for row in reports
        )
    by_mode = {
        mode: summarize(
            [row for row in reports if row["mode"] == mode], premium_model=premium_model
        )
        for mode in modes
    }
    by_mode_and_task = {
        mode: {
            task_id: summarize(
                [row for row in reports if row["mode"] == mode and row["task"] == task_id],
                premium_model=premium_model,
            )
            for task_id in tasks
        }
        for mode in modes
    }
    summary = {
        **manifest,
        "by_mode": by_mode,
        "by_mode_and_task": by_mode_and_task,
        "trials": reports,
    }
    save_json(args.output / "report.json", summary)
    save_json(args.output / "public-summary.json", build_public_summary(summary))
    print(json.dumps(summary["by_mode"], ensure_ascii=False), flush=True)
    return 0 if all(row["passed"] for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
