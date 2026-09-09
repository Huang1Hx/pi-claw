"""Pi-Claw 命令行入口，只使用 Python 标准库 argparse。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .agent import Agent
from .completion import ToolEvidenceCompletionVerifier
from .eval import EvalRunner, load_eval_suite, write_eval_report
from .policy import WorkspacePolicy
from .providers import OfflineCodingProvider, OpenAICompatibleProvider, ScriptedProvider
from .repl import CodingRepl
from .schemas import ModelReply, RunStatus, ToolCall
from .session import SessionStore
from .tools import ToolExecutor
from .trace import TraceReplay, TraceWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-claw",
        description="可审计、可回放、默认只读的本地 Coding Agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    chat = subparsers.add_parser("chat", help="启动可恢复、多轮交互的 Coding Agent")
    chat.add_argument("prompt", nargs="?", help="可选的首条任务；省略后进入交互输入")
    chat.add_argument("--workspace", type=Path, default=Path.cwd(), help="工作区目录")
    chat.add_argument(
        "--provider",
        choices=("scripted", "openai"),
        default=None,
        help="新会话默认 scripted；恢复会话默认沿用原 Provider",
    )
    chat.add_argument("--model", help="OpenAI-compatible 模型名，也可使用 OPENAI_MODEL")
    chat.add_argument("--base-url", help="OpenAI-compatible API 根地址")
    resume_group = chat.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--continue",
        "-c",
        action="store_true",
        dest="continue_latest",
        help="恢复当前工作区最近会话",
    )
    resume_group.add_argument("--session", help="恢复指定 session id")
    chat.add_argument("--data-dir", type=Path, help="覆盖会话数据目录，主要用于测试")
    chat.add_argument("--print", action="store_true", dest="print_only", help="执行首条任务后退出")
    chat.add_argument("--yes", action="store_true", help="自动批准策略允许的副作用，仅限可信工作区")
    chat.add_argument(
        "--max-steps", type=_positive_int, default=12, help="每个用户回合最大模型轮数"
    )
    chat.add_argument("--trace", type=Path, help="可选 JSONL 审计轨迹路径")

    sessions = subparsers.add_parser("sessions", help="列出当前工作区的历史会话")
    sessions.add_argument("--workspace", type=Path, default=Path.cwd(), help="工作区目录")
    sessions.add_argument("--data-dir", type=Path, help="覆盖会话数据目录，主要用于测试")

    demo = subparsers.add_parser("demo", help="在临时目录运行确定性的离线修复演示")
    demo.add_argument(
        "--yes",
        action="store_true",
        help="批准演示中的白名单写入和测试命令；仅对可信工作区使用",
    )
    demo.add_argument("--max-steps", type=_positive_int, default=8, help="最大模型轮数（默认：8）")
    demo.add_argument("--trace", type=Path, help="可选 JSONL 轨迹输出路径")

    run = subparsers.add_parser("run", help="在指定工作区运行 Agent")
    run.add_argument("task", help="交给 Agent 的任务文本")
    run.add_argument("--workspace", type=Path, default=Path.cwd(), help="工作区目录")
    run.add_argument(
        "--provider",
        choices=("scripted", "openai"),
        default="scripted",
        help="模型来源（默认：scripted，完全离线）",
    )
    run.add_argument("--model", help="OpenAI-compatible 模型名，也可使用 OPENAI_MODEL")
    run.add_argument("--base-url", help="OpenAI-compatible API 根地址")
    run.add_argument("--max-steps", type=_positive_int, default=8, help="最大模型轮数（默认：8）")
    run.add_argument("--trace", type=Path, help="可选 JSONL 轨迹输出路径")
    run.add_argument(
        "--yes",
        action="store_true",
        help="批准策略白名单内的写入和命令；仅对可信工作区使用",
    )

    replay = subparsers.add_parser("replay", help="只读打印历史 JSONL 轨迹，不执行工具")
    replay.add_argument("trace", type=Path, help="JSONL 轨迹路径")

    evaluate = subparsers.add_parser("eval", help="运行版本化、无网络的工程评测基线")
    evaluate.add_argument("--suite", type=Path, help="自定义评测集 JSON；默认使用包内基线")
    evaluate.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="只运行指定 case，可重复传入",
    )
    evaluate.add_argument("--output", type=Path, help="JSON 评测报告路径")
    evaluate.add_argument("--traces-dir", type=Path, help="每个 case 的 JSONL 轨迹目录")
    evaluate.add_argument(
        "--workers",
        type=_positive_int,
        default=1,
        help="本机并发 case 数（默认：1；线程仅并发独立工作区）",
    )
    evaluate.add_argument("--json", action="store_true", help="在终端打印完整 JSON 报告")
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _trace(path: Path | None) -> TraceWriter:
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = Path.cwd() / "runs" / f"pi-claw-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"
    # trace 是宿主审计输出，不属于 Agent 工具写权限；显式解析后便于稳定展示位置。
    return TraceWriter(path.expanduser().resolve(strict=False))


def _approval(enabled: bool):
    return (lambda *_: True) if enabled else None


def _warn_yes(enabled: bool) -> None:
    if enabled:
        print(
            "警告：--yes 会运行工作区代码；零依赖首版不提供进程沙箱，仅对可信工作区使用。",
            file=sys.stderr,
        )


def _run_demo(args: argparse.Namespace) -> int:
    _warn_yes(args.yes)
    trace = _trace(args.trace)
    print(f"审计轨迹：{trace.path}")
    with tempfile.TemporaryDirectory(prefix="pi-claw-demo-") as directory:
        workspace = Path(directory)
        calculator = workspace / "calculator.py"
        test_calculator = workspace / "test_calculator.py"
        calculator.write_text(
            'def add(a: int, b: int) -> int:\n    """返回两个整数之和。"""\n    return a - b\n',
            encoding="utf-8",
        )
        test_calculator.write_text(
            "import unittest\n\n"
            "from calculator import add\n\n\n"
            "class CalculatorTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        provider = ScriptedProvider(
            [
                ModelReply.call(ToolCall("demo-read", "read_file", {"path": "calculator.py"})),
                ModelReply.call(
                    ToolCall(
                        "demo-write",
                        "write_or_replace",
                        {
                            "path": "calculator.py",
                            "old": "    return a - b\n",
                            "new": "    return a + b\n",
                        },
                    )
                ),
                ModelReply.call(
                    ToolCall(
                        "demo-test",
                        "run_command",
                        {
                            "argv": [
                                sys.executable,
                                "-m",
                                "unittest",
                                "-v",
                                "test_calculator.py",
                            ]
                        },
                    )
                ),
                ModelReply.final(
                    "离线演示已结束。写入和测试是否成功以 ToolResult 与轨迹为准；"
                    "未经批准的操作不会被表述为成功。"
                ),
            ]
        )
        policy = WorkspacePolicy(
            workspace,
            allow_write=args.yes,
            allow_commands=args.yes,
        )
        executor = ToolExecutor(policy, approval_callback=_approval(args.yes))
        result = Agent(
            provider,
            executor,
            trace=trace,
            max_steps=args.max_steps,
            completion_verifier=ToolEvidenceCompletionVerifier(),
        ).run("检查 calculator.py，修复 add，并运行单元测试。")
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        if args.yes:
            print("演示已使用显式批准；临时工作区将在退出时清理。")
        else:
            print("默认只读：写入和命令审批被拒绝；使用 --yes 可体验完整闭环。")
        return 0 if result.status is RunStatus.COMPLETED else 1


def _run_agent(args: argparse.Namespace) -> int:
    _warn_yes(args.yes)
    workspace = args.workspace.expanduser().resolve()
    trace = _trace(args.trace)
    print(f"审计轨迹：{trace.path}")
    if args.provider == "openai":
        provider = OpenAICompatibleProvider(model=args.model, base_url=args.base_url)
    else:
        # 通用 scripted 模式只做一次有界只读枚举，然后诚实结束，适合离线冒烟测试。
        provider = ScriptedProvider(
            [
                ModelReply.call(ToolCall("scripted-list", "list_files", {"path": "."})),
                ModelReply.final(
                    "离线 scripted 运行已结束；已完成只读文件枚举，未声称修改工作区。"
                ),
            ]
        )
    policy = WorkspacePolicy(workspace, allow_write=args.yes, allow_commands=args.yes)
    executor = ToolExecutor(policy, approval_callback=_approval(args.yes))
    result = Agent(
        provider,
        executor,
        trace=trace,
        max_steps=args.max_steps,
        completion_verifier=ToolEvidenceCompletionVerifier(),
    ).run(args.task)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status is RunStatus.COMPLETED else 1


def _run_chat(args: argparse.Namespace) -> int:
    _warn_yes(args.yes)
    workspace = args.workspace.expanduser().resolve(strict=True)
    trace = _trace(args.trace)
    print(f"审计轨迹：{trace.path}")
    store = SessionStore(args.data_dir)
    resumed = None
    if args.session:
        resumed = store.load(args.session, workspace)
    elif args.continue_latest:
        resumed = store.latest(workspace)
    provider_name = args.provider or (resumed.provider if resumed is not None else "scripted")
    model = args.model or (resumed.model if resumed is not None else None)
    base_url = args.base_url or (resumed.base_url if resumed is not None else None)
    if provider_name == "openai":
        model = model or os.environ.get("OPENAI_MODEL")
        base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    if resumed is not None and args.provider and args.provider != resumed.provider:
        print(
            f"提示：会话 Provider 将从 {resumed.provider} 切换为 {args.provider}。",
            file=sys.stderr,
        )
    provider = (
        OpenAICompatibleProvider(model=model, base_url=base_url)
        if provider_name == "openai"
        else OfflineCodingProvider()
    )
    repl = CodingRepl(
        workspace=workspace,
        provider=provider,
        provider_name=provider_name,
        model=model,
        base_url=base_url,
        store=store,
        trace=trace,
        max_steps=args.max_steps,
        session_id=resumed.session_id if resumed is not None else args.session,
        continue_latest=args.continue_latest and resumed is None,
        auto_approve=args.yes,
    )
    return repl.run(args.prompt, print_only=args.print_only)


def _list_sessions(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve(strict=True)
    sessions = SessionStore(args.data_dir).list_sessions(workspace)
    if not sessions:
        print("当前工作区没有会话。")
        return 0
    for info in sessions:
        model = f"/{info.model}" if info.model else ""
        print(f"{info.session_id}\t{info.updated_at}\t{info.provider}{model}\t{info.title}")
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    replay = TraceReplay(args.trace)
    for line in replay.format_lines():
        print(line)
    return 0


def _run_eval(args: argparse.Namespace) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (
        args.output.expanduser().resolve(strict=False)
        if args.output is not None
        else (Path.cwd() / "runs" / f"eval-{stamp}-{uuid.uuid4().hex[:8]}.json").resolve()
    )
    traces_dir = (
        args.traces_dir.expanduser().resolve(strict=False)
        if args.traces_dir is not None
        else output.with_suffix("").with_name(output.stem + "-traces")
    )
    suite = load_eval_suite(args.suite)
    report = EvalRunner(suite, traces_dir=traces_dir).run(
        case_ids=set(args.case_ids) if args.case_ids else None,
        workers=args.workers,
    )
    report_path = write_eval_report(report, output)
    metrics = report.metrics
    security_rate = metrics["security_pass_rate"]
    security_text = (
        f"{security_rate:.1%}" if security_rate is not None else "N/A（未选择安全 case）"
    )
    print(
        f"评测集：{report.suite_name} v{report.suite_version}\n"
        f"结果：{metrics['passed_cases']}/{metrics['total_cases']} cases 通过 "
        f"({metrics['pass_rate']:.1%})\n"
        f"安全场景通过率：{security_text}\n"
        f"工具调用：{metrics['total_tool_calls']}，策略拒绝：{metrics['policy_denials']}，"
        f"审批拒绝：{metrics['approval_denials']}\n"
        f"并发：{metrics['execution_backend']}，workers={metrics['workers_used']}/"
        f"{metrics['workers_requested']}，最大同时运行={metrics['max_parallel_cases']}，"
        f"隔离违规={metrics['task_isolation_violations']}\n"
        f"完成验收拒绝：{metrics['completion_rejections']}，"
        f"未验证终态：{metrics['unverified_runs']}\n"
        f"报告：{report_path}\n轨迹目录：{traces_dir}"
    )
    for case in report.cases:
        state = "PASS" if case.passed else "FAIL"
        print(f"[{state}] {case.case_id}: {case.description}")
        if not case.passed:
            for assertion in case.assertions:
                if not assertion.passed:
                    print(f"  - {assertion.name}: {assertion.detail}")
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "chat":
            return _run_chat(args)
        if args.command == "sessions":
            return _list_sessions(args)
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "run":
            return _run_agent(args)
        if args.command == "replay":
            return _run_replay(args)
        if args.command == "eval":
            return _run_eval(args)
    except (OSError, TypeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    parser.error("未知命令")
    return 2
