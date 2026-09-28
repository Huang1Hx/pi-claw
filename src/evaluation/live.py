"""真实模型评测入口；默认只校准评分器，--live 才会调用已配置的模型。"""

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

from config import load_settings
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.context import ContextManager
from core.prompts import build_system_prompt
from evaluation.grading import GRADER_VERSION, calibrate, safe_function
from evaluation.report import build_report, summarize
from evaluation.tasks import INITIAL, TASKS, VISIBLE_TEST
from persistence.trace import TraceWriter, redact
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from security.workspace import Workspace
from tools import ToolExecutor

VERIFY_ARGV = ["uv", "run", "python", "-m", "unittest", "-q", "test_auth.py"]


def save_json(path: Path, data) -> None:
    """产物只包含脱敏后的任务、结果与用量；不序列化 Settings 中的连接密钥。"""
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2), encoding="utf-8")


def approval_for(task: str, workspace: Path):
    """本次评测授权仅限 auth.py 编辑和精确测试命令；运行候选前再做纯函数约束检查。"""

    def approve(call, decision, preview=None):
        """不覆盖 Policy 的 DENY，不让模型修改测试，也不接受任意 shell/测试入口。"""
        if task != "fix_auth":
            return False
        args = call.arguments
        if call.name in {"write_or_replace", "apply_patch"}:
            return args.get("path") in {"auth.py", "./auth.py"}
        if call.name != "run_command" or args.get("argv") != VERIFY_ARGV:
            return False
        try:
            # 比较真实目录而非参数拼写；继续沿用生产路径边界，拒绝穿越及其他目录。
            boundary = Workspace(workspace)
            if boundary.resolve(args.get("cwd", ".")) != boundary.root:
                return False
            if (workspace / "test_auth.py").read_text() != VISIBLE_TEST:
                return False
            safe_function((workspace / "auth.py").read_text())
        except (ValueError, SyntaxError, OSError, TypeError):
            return False
        return True

    return approve


def worker(args) -> None:
    """子进程使用原 Agent/路由/工具/上下文/验收；独立工作区，没有前次会话或 Git 历史。"""
    folder = args.output.resolve()
    workspace = folder / "workspace"
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    settings = replace(
        settings,
        profiles={
            name: replace(p, timeout_seconds=90, max_output_tokens=2048)
            for name, p in settings.profiles.items()
        },
        routing=replace(settings.routing, max_requests=8),
        workflow=replace(
            settings.workflow, max_planning_requests=2, max_review_requests=2, max_review_rounds=1
        ),
    )
    provider = RoutedProvider(settings, mode=args.route, max_attempts=1)
    save_json(
        folder / "config-snapshot.json",
        {
            "route": args.route,
            "models": {
                name: {
                    "model": p.model,
                    "thinking_mode": p.thinking_mode,
                    "thinking_format": p.thinking_format,
                }
                for name, p in settings.profiles.items()
            },
            "limits": {
                "max_steps": 8,
                "logical_requests": 8,
                "max_attempts": 1,
                "output_tokens_per_request": 2048,
                "http_idle_timeout_seconds": 90,
            },
            "workflow_enabled": settings.workflow.enabled,
        },
    )
    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True, allow_commands=True, command_profile="coding"),
        approval_callback=approval_for(args.worker, workspace),
        command_timeout=10,
        max_write_bytes=8000,
    )
    started = time.monotonic()
    first_text = None

    def on_event(event):
        """展示关键可观察事件，不打印 Key、模型私有推理或工具参数半成品。"""
        nonlocal first_text
        if event.type == "text_delta" and event.data.get("text", "").strip() and first_text is None:
            first_text = time.monotonic() - started
        if event.type in {
            "model_routed",
            "tool_result",
            "workflow_stage",
            "completion_rejected",
            "provider_error",
        }:
            print(
                json.dumps(
                    {
                        "t": round(time.monotonic() - started, 2),
                        "event": event.type,
                        "data": redact(event.data),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    def on_message(message):
        """公共消息持久化用于复核；私有 reasoning_content 不导出到评测轨迹。"""
        public = {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "tool_calls", "tool_call_id", "name"}
        }
        with (folder / "transcript.jsonl").open("a") as stream:
            stream.write(json.dumps(redact(public), ensure_ascii=False) + "\n")

    agent = Agent(
        provider,
        executor,
        max_steps=8,
        trace=TraceWriter(folder / "trace.jsonl"),
        system_prompt=build_system_prompt(workspace),
        context_manager=ContextManager(),
        completion_verifier=ToolEvidenceCompletionVerifier(),
        streaming=True,
        on_event=on_event,
        on_message=on_message,
    )
    result = agent.run(TASKS[args.worker])
    save_json(
        folder / "agent-result.json",
        {**result.to_dict(), "first_nonempty_text_seconds": first_text},
    )


def run_trial(task: str, number: int, args, route: str | None = None) -> dict:
    """父进程控制整个 trial 的墙钟期限；即使超时也给最终文件独立评分，并保留失败分母。"""
    route = route or args.route
    root = args.output / route if args.compare else args.output
    folder = root / f"{task}-{number}"
    folder.mkdir(parents=True)
    workspace = folder / "workspace"
    workspace.mkdir()
    (workspace / "auth.py").write_text(INITIAL)
    (workspace / "test_auth.py").write_text(VISIBLE_TEST)
    save_json(folder / "task.json", {"task": task, "prompt": TASKS[task], "trial": number})
    print(f"[开始] {task} trial={number} route={route}", flush=True)
    started = time.monotonic()
    timed_out = False
    command = [
        sys.executable,
        "-m",
        "evaluation.live",
        "--worker",
        task,
        "--output",
        str(folder.resolve()),
        "--config",
        str(args.config.resolve()),
        "--route",
        route,
    ]
    # 持续落盘的日志可在任务尚未完成时查看；期限到时回收整个子进程组。
    with (folder / "progress.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
        try:
            process.wait(timeout=args.deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    log_path = folder / "progress.log"
    log_path.write_text(redact(log_path.read_text(errors="replace")))
    result_path = folder / "agent-result.json"
    if not result_path.exists():
        save_json(
            result_path,
            {
                "status": "deadline" if timed_out else "harness_error",
                "final_text": "",
                "process_returncode": process.returncode,
            },
        )
    report = build_report(
        folder,
        {
            "task": task,
            "trial": number,
            "route": route,
            "duration_seconds": round(time.monotonic() - started, 2),
        },
    )
    save_json(folder / "grade.json", report)
    print(
        f"[评分] {task} trial={number} route={route}: {report['failure_category']}; "
        f"{report['passed_checks']}/{report['total_checks']} checks; {report['duration_seconds']}s",
        flush=True,
    )
    return report


def main() -> int:
    """显式 opt-in 真实调用；每批新输出目录防覆盖，记录源码摘要并先校准评分器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("pi-claw.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--route", default="primary", help="auto 或已配置的 profile 名")
    parser.add_argument("--compare", nargs="+", help="对照多个 profile，共用同一套题与预算")
    parser.add_argument("--regrade", type=Path, help="旧 report.json；只读原产物在新目录重评分")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=240)
    parser.add_argument("--task", choices=tuple(TASKS), action="append")
    parser.add_argument("--worker", choices=tuple(TASKS), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    if not 1 <= args.trials <= 5 or not 1 <= args.workers <= 2 or not 10 <= args.deadline <= 300:
        parser.error("trials 为 1–5；workers 为 1–2；deadline 为 10–300 秒")
    if args.regrade and args.live:
        parser.error("--regrade 不得与 --live 一起使用")
    routes = list(dict.fromkeys(args.compare or [args.route]))
    if args.live:
        settings = load_settings(
            config_path=args.config.resolve(), cwd=args.config.resolve().parent
        )
        if any(route != "auto" and route not in settings.profiles for route in routes):
            parser.error("route 必须为 auto 或配置中存在的 profile")
        if any(not route.replace("_", "").replace("-", "").isalnum() for route in routes):
            parser.error("评测 profile 名仅接受字母、数字、下划线和连字符")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止真实模型请求")
    print("[校准] 正确参考通过；原始漏洞和拒绝全部登录的伪修复均被检出。", flush=True)
    if args.regrade:
        old = json.loads(args.regrade.read_text())
        reports = [
            build_report(
                Path(r["workspace"]).parent,
                {
                    key: r[key]
                    for key in ("task", "trial", "route", "duration_seconds", "agent_status")
                },
            )
            for r in old["trials"]
        ]
        manifest = {
            "grader_version": GRADER_VERSION,
            "network_used": False,
            "regraded_from": str(args.regrade.resolve()),
            "original_source_sha256": old.get("source_sha256"),
        }
        write_batch(args.output, manifest, reports)
        print("[复核] 新成绩已写入新目录；未修改旧报告，未调用模型。", flush=True)
        return 0
    if not args.live:
        print("未指定 --live：未调用模型。", flush=True)
        return 0
    # 不含 .env/TOML 内容；源码摘要使未提交分支上的测量也可定位。
    source_root = Path(__file__).resolve().parents[1]
    fingerprint = sha256(
        b"".join(
            p.relative_to(source_root).as_posix().encode() + p.read_bytes()
            for p in sorted(source_root.rglob("*.py"))
        )
    ).hexdigest()
    manifest = {
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": fingerprint,
        "python": sys.version,
        "tasks": args.task or list(TASKS),
        "trials_per_task": args.trials,
        "route": args.route,
        "routes": routes,
        "workers": args.workers,
        "deadline_seconds": args.deadline,
        "scope": "受限小样本；纯函数限制；非生产能力基准；不写用户项目或持久会话账本",
    }
    save_json(args.output / "manifest.json", manifest)
    jobs = [
        (task, n, route)
        for task in dict.fromkeys(args.task or TASKS)
        for n in range(1, args.trials + 1)
        for route in routes
    ]
    print(
        f"[预算] {len(jobs)} trials；每 trial ≤8 次逻辑请求、每次输出 ≤2048 tokens；"
        f"并发 {args.workers}，期限 {args.deadline}s；输入 Token 不设总量封顶。",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_trial, task, n, args, route) for task, n, route in jobs]
        reports = [future.result() for future in futures]
    summary = write_batch(args.output, manifest, reports)
    print(json.dumps(summary["by_route"], ensure_ascii=False), flush=True)
    return 0 if all(r["passed"] for r in reports) else 1


def write_batch(output: Path, manifest: dict, reports: list[dict]) -> dict:
    """按模型汇总，另生成可直接跳转 Trace 的失败清单。"""
    summary = {
        **manifest,
        "trials": reports,
        "by_route": {
            route: summarize([r for r in reports if r["route"] == route])
            for route in dict.fromkeys(r["route"] for r in reports)
        },
    }
    save_json(output / "report.json", summary)
    save_json(
        output / "failure-index.json",
        [
            {
                "task": r["task"],
                "trial": r["trial"],
                "route": r["route"],
                "scope": "trial_check",
                **failure,
            }
            for r in reports
            for failure in r["failures"]
        ]
        + [
            {
                "task": r["task"],
                "trial": r["trial"],
                "route": r["route"],
                "scope": "process_event",
                "trial_passed": r["passed"],
                **issue,
            }
            for r in reports
            for issue in r["process_issues"]
        ],
    )
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
