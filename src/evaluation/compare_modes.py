"""用同一任务、评分器和预算比较单 Agent、顺序工作流与主从多 Agent。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
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


def save_json(path: Path, data) -> None:
    """所有评测产物统一脱敏；Settings、Key 与 URL 不进入报告。"""
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


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
        except (OSError, SyntaxError, TypeError, ValueError):
            return False
        return True

    return approve


def _provider(settings, profile: str) -> RoutedProvider:
    """每个 Agent 使用独立 Provider 状态；相同连接仅共享配置值。"""
    return RoutedProvider(settings, mode=profile, max_attempts=1)


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
            "max_attempts_per_request": 1,
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
            _provider(settings, settings.workflow.coder),
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
            RoutedProvider(workflow_settings, mode="auto", max_attempts=1),
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
            _provider(settings, settings.workflow.coder),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        )
        implementation = _agent(
            _provider(settings, settings.workflow.planner),
            _executor(workspace, args.worker, writable=False),
            folder / "trace-subagent-implementation.jsonl",
            workspace,
            steps=6,
        )
        risks = _agent(
            _provider(settings, settings.workflow.reviewer),
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
    expected = {"leader"}
    if mode == "team":
        expected |= {"subagent-implementation", "subagent-risks"}
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
    models = sorted(
        {
            event["payload"]["model"]
            for event in events
            if event["event"] == "model_routed" and isinstance(event["payload"].get("model"), str)
        }
    )
    return {
        **metadata,
        "grader_version": GRADER_VERSION,
        "passed": passed,
        "leader_status": leader["status"],
        "checks": checks,
        "passed_checks": sum(item["passed"] is True for item in checks),
        "total_checks": len(checks),
        "usage": usage,
        "usage_may_be_incomplete": usage is None
        or bool(usage.get("unknown_participants"))
        or bool(usage.get("requests_without_usage")),
        "duration_seconds": result.get("duration_seconds", metadata["duration_seconds"]),
        "models_used": models,
        "participants": 1 + len(result.get("subagents", [])),
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


def summarize(rows: list[dict]) -> dict:
    """汇总成功、可靠性、端到端耗时、主从总 Token、冲突、越权和返工。"""
    successes = sum(row["passed"] for row in rows)
    known_tokens = sum((row["usage"] or {}).get("total_tokens", 0) for row in rows)
    durations = [row["duration_seconds"] for row in rows]
    return {
        "models_used": sorted({model for row in rows for model in row["models_used"]}),
        "reliability": reliability(rows),
        "median_duration_seconds": round(median(durations), 3),
        "total_duration_seconds": round(sum(durations), 3),
        "known_total_tokens": known_tokens,
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
    ]
    started = time.monotonic()
    timed_out = False
    with (folder / "progress.log").open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=stream, start_new_session=True)
        try:
            process.wait(timeout=args.deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
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


def main() -> int:
    """默认只校准；--live 才运行真实模型，输出目录必须不存在。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("pi-claw.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=420)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--task", choices=tuple(TASKS), action="append")
    parser.add_argument("--mode", choices=MODES, action="append")
    parser.add_argument("--worker", choices=tuple(TASKS), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        args.mode = args.mode[0]
        worker(args)
        return 0
    if not 1 <= args.trials <= 5 or not 1 <= args.workers <= 3:
        parser.error("trials 为 1–5；workers 为 1–3")
    if not 30 <= args.deadline <= 900 or not 4 <= args.max_steps <= 40:
        parser.error("deadline 为 30–900 秒；max-steps 为 4–40")
    modes = list(dict.fromkeys(args.mode or MODES))
    tasks = list(dict.fromkeys(args.task or TASKS))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止真实模型请求")
    print("[校准] 两项参考实现通过，初始缺陷均被隐藏用例检出。", flush=True)
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
    manifest = {
        "schema_version": 1,
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
        "models": {
            role: settings.profiles[getattr(settings.workflow, role)].model
            for role in ("planner", "coder", "reviewer")
        },
        "accounting": "Token 统计包含主 Agent、全部子 Agent、工作流阶段与返工；不计算货币费用",
    }
    save_json(args.output / "manifest.json", manifest)
    jobs = [
        (mode, task_id, number)
        for task_id in tasks
        for number in range(1, args.trials + 1)
        for mode in modes
    ]
    print(
        f"[批次] {len(jobs)} trials；modes={modes}；tasks={tasks}；并发={args.workers}；"
        f"每 trial 期限={args.deadline}s。",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run_trial, mode, task_id, number, args) for mode, task_id, number in jobs
        ]
        reports = [future.result() for future in futures]
    summary = {
        **manifest,
        "by_mode": {
            mode: summarize([row for row in reports if row["mode"] == mode]) for mode in modes
        },
        "trials": reports,
    }
    save_json(args.output / "report.json", summary)
    print(json.dumps(summary["by_mode"], ensure_ascii=False), flush=True)
    return 0 if all(row["passed"] for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
