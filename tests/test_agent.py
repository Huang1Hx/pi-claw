from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pi_claw.agent import Agent
from pi_claw.policy import WorkspacePolicy
from pi_claw.providers import ProviderError, ScriptedProvider
from pi_claw.schemas import ModelReply, RunStatus, ToolCall
from pi_claw.tools import ToolExecutor
from pi_claw.trace import TraceReplay, TraceWriter


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
        "model_reply",
        "tool_call",
        "tool_result",
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
