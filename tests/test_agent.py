from __future__ import annotations

from pathlib import Path

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
