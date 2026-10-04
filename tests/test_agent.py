from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.control import ExecutionControl
from core.schemas import ModelReply, RunStatus, ToolCall
from persistence.trace import TraceReplay, TraceWriter
from providers import ProviderError, ScriptedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def test_agent_happy_path_records_tool_and_final(workspace: Path, tmp_path: Path) -> None:
    trace_path = tmp_path / "happy.jsonl"
    provider = ScriptedProvider(
        [
            ModelReply.call(ToolCall("read-1", "read_file", {"path": "alpha.txt"})),
            ModelReply.final("检查完成"),
        ]
    )
    result = Agent(
        provider,
        ToolExecutor(WorkspacePolicy(workspace)),
        trace=TraceWriter(trace_path),
    ).run("读取 alpha.txt")

    assert result.status is RunStatus.COMPLETED
    assert result.steps == 2
    assert result.final_text == "检查完成"
    events = TraceReplay(trace_path).read()
    assert [event["event"] for event in events] == [
        "run_started",
        "usage_updated",
        "model_reply",
        "tool_call",
        "tool_result",
        "usage_updated",
        "model_reply",
        "run_completed",
    ]
    tool_result = next(event for event in events if event["event"] == "tool_result")
    assert tool_result["payload"]["ok"] is True


def test_agent_converts_provider_error_to_result(workspace: Path) -> None:
    class BrokenProvider:
        def complete(self, _messages: object, _tools: object) -> ModelReply:
            raise ProviderError("offline failure")

    result = Agent(BrokenProvider(), ToolExecutor(WorkspacePolicy(workspace))).run("task")
    assert result.status is RunStatus.PROVIDER_ERROR
    assert result.steps == 1
    assert "offline failure" in result.error


def test_agent_converts_keyboard_interrupt_to_cancelled_without_retry(workspace: Path) -> None:
    """终端 Ctrl+C 是用户取消，不应冒充 Provider 故障或自动再次请求。"""

    class InterruptedProvider:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages: object, _tools: object) -> ModelReply:
            self.calls += 1
            raise KeyboardInterrupt

    provider = InterruptedProvider()
    result = Agent(provider, ToolExecutor(WorkspacePolicy(workspace))).run("task")

    assert result.status is RunStatus.CANCELLED
    assert provider.calls == 1


def test_agent_checks_cancellation_after_custom_provider_returns(workspace: Path) -> None:
    """不感知 control 的自定义 Provider 返回后，也不能用完整文本覆盖取消终态。"""
    control = ExecutionControl()

    class CancellingProvider:
        def complete(self, _messages: object, _tools: object) -> ModelReply:
            control.cancel()
            return ModelReply.final("too late")

    result = Agent(CancellingProvider(), ToolExecutor(WorkspacePolicy(workspace))).run(
        "task", control=control
    )

    assert result.status is RunStatus.CANCELLED
    assert result.final_text is None


def test_agent_rejects_provider_returning_wrong_type(workspace: Path) -> None:
    class InvalidProvider:
        def complete(self, _messages: object, _tools: object) -> str:
            return "not a ModelReply"

    result = Agent(InvalidProvider(), ToolExecutor(WorkspacePolicy(workspace))).run("task")
    assert result.status is RunStatus.PROVIDER_ERROR
    assert "ModelReply" in result.error


def test_agent_stops_at_max_steps(workspace: Path) -> None:
    calls = [
        ModelReply.call(ToolCall(f"read-{index}", "read_file", {"path": "alpha.txt"}))
        for index in range(3)
    ]
    result = Agent(
        ScriptedProvider(calls),
        ToolExecutor(WorkspacePolicy(workspace)),
        max_steps=2,
    ).run("never finish")
    assert result.status is RunStatus.MAX_STEPS
    assert result.steps == 2
    assert "安全停止" in result.error


def test_repeated_policy_denial_records_policy_stall(workspace: Path, tmp_path: Path) -> None:
    """同一越权调用连续被拒绝时要有界停止，并在 Trace 标明策略停滞。"""
    trace_path = tmp_path / "policy-stall.jsonl"
    replies = [
        ModelReply.call(ToolCall(f"escape-{index}", "read_file", {"path": "../outside"}))
        for index in range(3)
    ]

    result = Agent(
        ScriptedProvider(replies),
        ToolExecutor(WorkspacePolicy(workspace)),
        trace=TraceWriter(trace_path),
        max_steps=8,
    ).run("读取工作区外文件")

    assert result.status is RunStatus.LOOP_DETECTED
    assert result.steps == 3
    stopped = [
        event for event in TraceReplay(trace_path).read() if event["event"] == "loop_detected"
    ]
    assert stopped[-1]["payload"]["stop_reason"] == "policy_stall"
    assert stopped[-1]["payload"]["tool_name"] == "read_file"
    assert "穿越" in stopped[-1]["payload"]["policy_reason"]
    assert ".." in stopped[-1]["payload"]["policy_reason"]


def test_repeated_non_whitelisted_command_stops_in_three_requests(workspace: Path) -> None:
    """危险命令被同一策略拒绝三次后终止，不消耗完整步骤或路由请求预算。"""
    replies = [
        ModelReply.call(ToolCall(f"unsafe-{index}", "run_command", {"argv": ["rm", "-rf", "."]}))
        for index in range(3)
    ]

    result = Agent(
        ScriptedProvider(replies),
        ToolExecutor(WorkspacePolicy(workspace, allow_commands=True)),
        max_steps=24,
    ).run("删除全部文件")

    assert result.status is RunStatus.LOOP_DETECTED
    assert result.steps == 3
    assert result.usage["requests"] == 3


def test_agent_persists_history_and_executes_multiple_tool_calls(
    workspace: Path,
) -> None:
    history: list[dict[str, object]] = []
    persisted: list[dict[str, object]] = []
    provider = ScriptedProvider(
        [
            ModelReply.calls(
                [
                    ToolCall("read-1", "read_file", {"path": "alpha.txt"}),
                    ToolCall("list-1", "list_files", {"path": "nested"}),
                ]
            ),
            ModelReply.final("first done"),
            ModelReply.final("second done"),
        ]
    )
    agent = Agent(
        provider,
        ToolExecutor(WorkspacePolicy(workspace)),
        system_prompt="system rules",
        on_message=persisted.append,
    )

    first = agent.run("first task", messages=history)
    second = agent.run("second task", messages=history)

    assert first.final_text == "first done"
    assert second.final_text == "second done"
    assert agent.last_messages is history
    assert [message["role"] for message in history] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assistant_call = history[2]
    assert len(assistant_call["tool_calls"]) == 2  # type: ignore[arg-type]
    assert [message["tool_call_id"] for message in history[3:5]] == ["read-1", "list-1"]
    assert persisted == history


def test_agent_safely_rejects_more_than_sixteen_tool_calls(workspace: Path) -> None:
    calls = [ToolCall(f"call-{index}", "list_files", {"path": "."}) for index in range(17)]
    history: list[dict[str, object]] = []
    result = Agent(
        ScriptedProvider([ModelReply.calls(calls)]),
        ToolExecutor(WorkspacePolicy(workspace)),
    ).run("too many", messages=history)

    assert result.status is RunStatus.FAILED
    assert "17 个工具调用" in result.error
    assert "上限 16" in result.error
    assert [message["role"] for message in history] == ["user"]


def test_agent_rejects_oversized_tool_arguments_before_execution(workspace: Path) -> None:
    history: list[dict[str, object]] = []
    result = Agent(
        ScriptedProvider([ModelReply.call(ToolCall("huge", "read_file", {"path": "x" * 2_000}))]),
        ToolExecutor(WorkspacePolicy(workspace)),
        max_tool_call_chars=200,
    ).run("oversized", messages=history)

    assert result.status is RunStatus.FAILED
    assert "工具调用参数超过字符上限" in result.error
    assert [message["role"] for message in history] == ["user"]


def test_agent_feeds_failed_tool_result_back_before_recovery(workspace: Path) -> None:
    class RecoveringProvider:
        def __init__(self) -> None:
            self.step = 0
            self.saw_failed_result = False

        def complete(
            self,
            messages: Sequence[dict[str, Any]],
            _tools: Sequence[dict[str, Any]] | None = None,
        ) -> ModelReply:
            self.step += 1
            if self.step == 1:
                return ModelReply.call(
                    ToolCall(
                        "bad-edit",
                        "write_or_replace",
                        {"path": "alpha.txt", "old": "不存在", "new": "changed"},
                    )
                )
            if self.step == 2:
                failed = json.loads(str(messages[-1]["content"]))
                self.saw_failed_result = failed["ok"] is False and "不存在" in failed["error"]
                return ModelReply.call(ToolCall("read", "read_file", {"path": "alpha.txt"}))
            if self.step == 3:
                observed = json.loads(str(messages[-1]["content"]))
                assert observed["ok"] is True
                return ModelReply.call(
                    ToolCall(
                        "good-edit",
                        "write_or_replace",
                        {"path": "alpha.txt", "old": "alpha", "new": "changed"},
                    )
                )
            return ModelReply.final("已根据失败结果重新读取并完成修改")

    provider = RecoveringProvider()
    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True),
        approval_callback=lambda *_: True,
    )
    result = Agent(provider, executor).run("修复 alpha.txt")

    assert result.status is RunStatus.COMPLETED
    assert provider.saw_failed_result is True
    assert (workspace / "alpha.txt").read_text(encoding="utf-8").startswith("changed")


def test_agent_validates_task_and_max_steps(workspace: Path) -> None:
    provider = ScriptedProvider([ModelReply.final("done")])
    executor = ToolExecutor(WorkspacePolicy(workspace))
    try:
        Agent(provider, executor, max_steps=0)
    except ValueError as exc:
        assert "max_steps" in str(exc)
    else:
        raise AssertionError("max_steps=0 应被拒绝")

    agent = Agent(provider, executor)
    try:
        agent.run("   ")
    except ValueError as exc:
        assert "task" in str(exc)
    else:
        raise AssertionError("空任务应被拒绝")


def test_agent_rejects_unverified_final_then_accepts_after_test(
    workspace: Path,
    tmp_path: Path,
) -> None:
    (workspace / "test_alpha.py").write_text(
        "import unittest\n\n"
        "class AlphaTest(unittest.TestCase):\n"
        "    def test_changed(self):\n"
        "        self.assertTrue(open('alpha.txt').read().startswith('changed'))\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "completion.jsonl"
    provider = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "write",
                    "write_or_replace",
                    {"path": "alpha.txt", "old": "alpha", "new": "changed"},
                )
            ),
            ModelReply.final("已经修改完成"),
            ModelReply.call(
                ToolCall(
                    "test",
                    "run_command",
                    {"argv": [sys.executable, "-m", "unittest", "-v", "test_alpha.py"]},
                )
            ),
            ModelReply.final("修改完成并通过测试"),
        ]
    )
    history: list[dict[str, Any]] = []
    result = Agent(
        provider,
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=True, allow_commands=True),
            approval_callback=lambda *_: True,
        ),
        trace=TraceWriter(trace_path),
        completion_verifier=ToolEvidenceCompletionVerifier(),
    ).run("修改 alpha", messages=history)

    assert result.status is RunStatus.COMPLETED
    assert result.verification is not None
    assert result.verification["accepted"] is True
    assert result.verification["checks"][0]["kind"] == "test"
    host_feedback = json.loads(str(history[-4]["content"]))
    assert host_feedback["type"] == "host_completion_rejected"
    events = TraceReplay(trace_path).read()
    assert [event["event"] for event in events].count("completion_rejected") == 1
    assert any(event["event"] == "completion_verified" for event in events)


def test_agent_returns_unverified_when_final_has_no_post_write_check(
    workspace: Path,
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "unverified.jsonl"
    result = Agent(
        ScriptedProvider(
            [
                ModelReply.call(
                    ToolCall(
                        "write",
                        "write_or_replace",
                        {"path": "alpha.txt", "old": "alpha", "new": "changed"},
                    )
                ),
                ModelReply.final("修改完成"),
            ]
        ),
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=True),
            approval_callback=lambda *_: True,
        ),
        trace=TraceWriter(trace_path),
        max_steps=2,
        completion_verifier=ToolEvidenceCompletionVerifier(),
    ).run("修改 alpha")

    assert result.status is RunStatus.UNVERIFIED
    assert not result.ok
    assert result.verification is not None
    assert result.verification["accepted"] is False
    assert [event["event"] for event in TraceReplay(trace_path).read()][-1] == "run_unverified"
