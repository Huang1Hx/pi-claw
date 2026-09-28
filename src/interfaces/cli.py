"""Pi-Claw 命令行入口，只使用 Python 标准库 argparse。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from config import load_settings
from config.workspace import resolve_workspace
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.schemas import ModelReply, RunStatus, ToolCall
from evaluation.runner import EvalRunner, load_eval_suite, write_eval_report
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceReplay, TraceWriter
from providers import OfflineCodingProvider, OpenAICompatibleProvider, ScriptedProvider
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def build_parser() -> argparse.ArgumentParser:
    """定义命令行参数与子命令。"""
    parser = argparse.ArgumentParser(
        prog="pi-claw",
        description="可审计、可回放、默认只读的本地 Coding Agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    chat = subparsers.add_parser("chat", help="启动可恢复、多轮交互的 Coding Agent")
    chat.add_argument("prompt", nargs="?", help="可选的首条任务；省略后进入交互输入")
    chat.add_argument("--workspace", type=Path, help="工作区目录；默认启动目录/projects，自动创建")
    chat.add_argument(
        "--provider",
        choices=("scripted", "openai", "routed"),
        default=None,
        help="新会话默认 scripted；恢复会话默认沿用原 Provider",
    )
    chat.add_argument("--model", help="OpenAI-compatible 模型名，也可使用 OPENAI_MODEL")
    chat.add_argument("--base-url", help="OpenAI-compatible API 根地址")
    chat.add_argument(
        "--no-stream", action="store_true", help="使用同步响应，兼容不支持 SSE 的服务"
    )
    chat.add_argument(
        "--max-attempts", type=_positive_int, default=3, help="模型请求最多尝试次数（1–10）"
    )
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
    _add_config_options(chat)

    sessions = subparsers.add_parser("sessions", help="列出当前工作区的历史会话")
    sessions.add_argument("--workspace", type=Path, help="工作区目录；默认启动目录/projects")
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
    run.add_argument("--workspace", type=Path, help="工作区目录；默认启动目录/projects，自动创建")
    run.add_argument(
        "--provider",
        choices=("scripted", "openai", "routed"),
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
    _add_config_options(run)

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
    routing_eval = subparsers.add_parser(
        "eval-routing", help="离线复杂度分流、执行边界与全主力调用分布对照"
    )
    routing_eval.add_argument("--output", type=Path, default=Path("runs/routing-eval.json"))
    return parser


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    """两个模型入口共享显式配置参数；.env 默认仅在启动目录加载。"""
    parser.add_argument("--config", type=Path, help="显式加载模型 profile TOML")
    parser.add_argument("--route", metavar="auto|PROFILE", help="启用路由或固定已配置 profile")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--env-file", type=Path, help="覆盖启动目录的 .env")
    group.add_argument("--no-env", action="store_true", help="不读取 .env，仍读取进程环境")


def _positive_int(value: str) -> int:
    """将命令行文本解析为正整数，供 argparse 在启动运行时前拒绝无效预算。"""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _trace(path: Path | None) -> TraceWriter:
    """采用用户指定路径或 runs 下唯一文件名创建审计写入器，返回解析后的可展示路径。"""
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = Path.cwd() / "runs" / f"pi-claw-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"
    # trace 是宿主审计输出，不属于 Agent 工具写权限；显式解析后便于稳定展示位置。
    return TraceWriter(path.expanduser().resolve(strict=False))


def _approval(enabled: bool):
    """仅当显式启用自动批准时返回同意回调；该回调不能绕过 Policy 的硬拒绝。"""
    return (lambda *_: True) if enabled else None


def _warn_yes(enabled: bool) -> None:
    """使用 --yes 时向标准错误提示执行仓库代码的风险；这不是沙箱授权。"""
    if enabled:
        print(
            "警告：--yes 会运行工作区代码；本程序不提供进程沙箱，仅对可信工作区使用。",
            file=sys.stderr,
        )


def _run_demo(args: argparse.Namespace) -> int:
    """在临时工作区运行固定修复脚本并写审计轨迹；默认拒绝副作用，完成返回 0。"""
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
    """装配一次性任务的 Provider、Policy、工具和验收器，输出 JSON 终态并返回退出码。"""
    _warn_yes(args.yes)
    workspace = resolve_workspace(args.workspace)
    trace = _trace(args.trace)
    print(f"审计轨迹：{trace.path}")
    if args.route or args.provider == "routed":
        provider = RoutedProvider(args.settings, mode=args.route or args.model or "auto")
    elif args.provider == "openai":
        env = args.settings.environment
        provider = OpenAICompatibleProvider(
            api_key=env.get("OPENAI_API_KEY", ""),
            model=args.model or env.get("OPENAI_MODEL", ""),
            base_url=args.base_url or env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        )
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
    """解析恢复会话和模型配置，装配 CodingRepl；密钥仅从本次启动配置取得。"""
    _warn_yes(args.yes)
    workspace = resolve_workspace(args.workspace)
    trace = _trace(args.trace)
    print(f"审计轨迹：{trace.path}")
    store = SessionStore(args.data_dir)
    resumed = None
    if args.session:
        resumed = store.load(args.session, workspace)
    elif args.continue_latest:
        resumed = store.latest(workspace)
    provider_name = args.provider or (resumed.provider if resumed is not None else "scripted")
    if args.route:
        provider_name = "routed"
    same_provider = resumed is not None and resumed.provider == provider_name
    model = args.model or (resumed.model if same_provider else None)
    base_url = args.base_url or (resumed.base_url if same_provider else None)
    if provider_name == "openai":
        model = model or args.settings.environment.get("OPENAI_MODEL")
        base_url = base_url or args.settings.environment.get("OPENAI_BASE_URL")
    if provider_name == "routed":
        model, base_url = args.route or model or "auto", None
    if resumed is not None and args.provider and args.provider != resumed.provider:
        print(
            f"提示：会话 Provider 将从 {resumed.provider} 切换为 {args.provider}。",
            file=sys.stderr,
        )
    if provider_name == "routed":
        provider = RoutedProvider(args.settings, mode=model, max_attempts=args.max_attempts)
    elif provider_name == "openai":
        provider = OpenAICompatibleProvider(
            api_key=args.settings.environment.get("OPENAI_API_KEY", ""),
            model=model,
            base_url=base_url,
            max_attempts=args.max_attempts,
        )
    else:
        provider = OfflineCodingProvider()
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
        streaming=not args.no_stream,
        settings=args.settings,
        max_attempts=args.max_attempts,
    )
    return repl.run(args.prompt, print_only=args.print_only)


def _list_sessions(args: argparse.Namespace) -> int:
    """只列出当前工作区的会话元数据，不加载模型或执行工具。"""
    workspace = resolve_workspace(args.workspace, create=False)
    if not workspace.exists():
        print("当前工作区没有会话。")
        return 0
    sessions = SessionStore(args.data_dir).list_sessions(workspace)
    if not sessions:
        print("当前工作区没有会话。")
        return 0
    for info in sessions:
        model = f"/{info.model}" if info.model else ""
        print(f"{info.session_id}\t{info.updated_at}\t{info.provider}{model}\t{info.title}")
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    """将已有 Trace 格式化输出；回放不重新调用模型，也不重放文件或命令操作。"""
    replay = TraceReplay(args.trace)
    for line in replay.format_lines():
        print(line)
    return 0


def _run_eval(args: argparse.Namespace) -> int:
    """运行所选离线 case，保存报告和轨迹；全部通过返回 0，否则返回 1。"""
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
    """解析命令并派发到对应界面流程，将可读错误转换为退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"chat", "run"}:
            if args.route and (args.model or args.base_url):
                raise ValueError("--route 与 --model/--base-url 不能混用；连接参数来自 profile")
            args.settings = load_settings(
                config_path=args.config, env_file=args.env_file, no_env=args.no_env
            )
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
        if args.command == "eval-routing":
            from evaluation.routing import evaluate_routing

            report = evaluate_routing(args.output.resolve())
            print(
                f"离线路由合约：{report['passed_cases']}/{report['total_cases']}；"
                "复杂度分流与执行边界验证（固定替身，非真实模型能力）\n"
                f"报告：{args.output.resolve()}"
            )
            return 0 if report["passed_cases"] == report["total_cases"] else 1
    except (OSError, TypeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    parser.error("未知命令")
    return 2
