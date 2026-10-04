"""主 Agent＋独立只读子 Agent 的隔离、屏障、交接与用量汇总。"""

import threading
import time

import pytest

from core.agent import Agent
from core.control import ExecutionControl
from core.schemas import ModelReply, RunStatus
from core.team import ReadOnlySubagentTeam, SubagentAssignment
from core.usage import TokenUsage
from providers import ScriptedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


class RecordingProvider:
    """记录上下文并用屏障证明两个子 Agent 真实重叠运行。"""

    def __init__(self, text, barrier=None):
        self.text = text
        self.barrier = barrier
        self.messages = []

    def complete(self, messages, tools=None):
        self.messages.append([dict(item) for item in messages])
        if self.barrier:
            self.barrier.wait(timeout=2)
            time.sleep(0.01)
        return ModelReply(final_text=self.text, usage=TokenUsage(10, 5, 15))


def make_agent(workspace, provider, *, writable=False):
    """测试实例仍走真实 Agent 与 Policy，只替换模型响应。"""
    return Agent(
        provider,
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=writable, allow_commands=writable),
            approval_callback=lambda *args: writable,
        ),
        max_steps=2,
        system_prompt="system",
    )


def test_team_runs_independent_children_before_leader_and_aggregates_usage(tmp_path):
    """主从上下文隔离，交接只含公开结论，总 Token 包含所有参与者。"""
    barrier = threading.Barrier(2)
    p1 = RecordingProvider("实现分析", barrier)
    p2 = RecordingProvider("测试分析", barrier)
    lead = RecordingProvider("完成")
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, lead, writable=True),
        {
            "implementation": make_agent(tmp_path, p1),
            "tests": make_agent(tmp_path, p2),
        },
    )
    result = team.run(
        "修复目标",
        (
            SubagentAssignment("implementation", "实现分析", "只读分析实现"),
            SubagentAssignment("tests", "测试分析", "只读分析测试"),
        ),
    )
    assert result.leader.ok and result.max_parallel_subagents == 2
    assert result.usage["total_tokens"] == 45
    leader_prompt = lead.messages[0][-1]["content"]
    assert "实现分析" in leader_prompt and "测试分析" in leader_prompt
    assert "只读分析测试" not in repr(p1.messages)
    assert result.to_dict()["subagents"][0]["result"]["run_id"]


def test_team_rejects_shared_or_writable_subagent(tmp_path):
    """实例复用会触发重入风险；子 Agent 获得写/命令能力也必须在启动前拒绝。"""
    shared = make_agent(tmp_path, ScriptedProvider([ModelReply.final("x")]))
    with pytest.raises(ValueError, match="独立实例"):
        ReadOnlySubagentTeam(shared, {"child": shared}, max_workers=1)
    writable = make_agent(tmp_path, ScriptedProvider([ModelReply.final("x")]), writable=True)
    with pytest.raises(ValueError, match="必须保持只读"):
        ReadOnlySubagentTeam(shared, {"child": writable}, max_workers=1)


def test_team_validates_exact_assignments(tmp_path):
    """协调器不接受重名、漏项或临时注入的未知子 Agent。"""
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, ScriptedProvider([ModelReply.final("lead")])),
        {"child": make_agent(tmp_path, ScriptedProvider([ModelReply.final("child")]))},
        max_workers=1,
    )
    with pytest.raises(ValueError, match="一一对应"):
        team.run("task", (SubagentAssignment("other", "role", "task"),))


def test_team_shares_outer_cancellation_with_children_and_leader(tmp_path):
    """取消属于整个团队运行，不能只停主 Agent 而留下子 Agent 继续请求。"""
    control = ExecutionControl()
    control.cancel()
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, ScriptedProvider([ModelReply.final("lead")])),
        {
            "a": make_agent(tmp_path, ScriptedProvider([ModelReply.final("a")])),
            "b": make_agent(tmp_path, ScriptedProvider([ModelReply.final("b")])),
        },
    )

    result = team.run(
        "task",
        (
            SubagentAssignment("a", "role", "task-a"),
            SubagentAssignment("b", "role", "task-b"),
        ),
        control=control,
    )

    assert result.leader.status is RunStatus.CANCELLED
    assert {item.result.status for item in result.subagents} == {RunStatus.CANCELLED}
    assert result.usage["requests"] == 0
