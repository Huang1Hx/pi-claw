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

from .agent import Agent
from .policy import WorkspacePolicy
from .providers import OpenAICompatibleProvider, ScriptedProvider
from .schemas import ModelReply, RunStatus, ToolCall
from .tools import ToolExecutor
from .trace import TraceReplay, TraceWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-claw",
        description="可审计、可回放、默认只读的本地 Coding Agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    ).run(args.task)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status is RunStatus.COMPLETED else 1


def _run_replay(args: argparse.Namespace) -> int:
    replay = TraceReplay(args.trace)
    for line in replay.format_lines():
        print(line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "run":
            return _run_agent(args)
        if args.command == "replay":
            return _run_replay(args)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    parser.error("未知命令")
    return 2
