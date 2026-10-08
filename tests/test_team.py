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


def test_assignment_work_key_is_stable_and_field_sensitive():
    """work_key 必须对 objective/scope/deliverable 敏感，且与 scope 顺序无关。"""
    base = SubagentAssignment(
        "a",
        "分析",
        "只读分析",
        objective="分析 solution.py",
        scope=("SPEC.md", "solution.py"),
        deliverable="implementation_advice",
    )
    same = SubagentAssignment(
        "b",
        "分析",
        "只读分析",
        objective="分析 solution.py",
        scope=("solution.py", "SPEC.md"),
        deliverable="implementation_advice",
    )
    other_deliverable = SubagentAssignment(
        "c",
        "分析",
        "只读分析",
        objective="分析 solution.py",
        scope=("SPEC.md", "solution.py"),
        deliverable="risk_and_edge_cases",
    )
    other_objective = SubagentAssignment(
        "d",
        "分析",
        "只读分析",
        objective="分析 test_target.py",
        scope=("SPEC.md", "solution.py"),
        deliverable="implementation_advice",
    )
    assert base.work_key == same.work_key
    assert base.work_key != other_deliverable.work_key
    assert base.work_key != other_objective.work_key


def test_legacy_task_only_assignments_share_key_when_identical():
    """未提供扩展字段时退化为 role/task，两份完全相同的任务必须被判为重复。"""
    a = SubagentAssignment("a", "role", "same task")
    b = SubagentAssignment("b", "role", "same task")
    c = SubagentAssignment("c", "role", "different task")
    assert a.work_key == b.work_key
    assert a.work_key != c.work_key


def test_duplicate_unique_assignment_is_blocked_before_model_call(tmp_path):
    """unique 策略下重复任务只运行一个，另一个不得触发模型调用。"""
    p1 = RecordingProvider("结论一")
    p2 = RecordingProvider("结论二")
    lead = RecordingProvider("完成")
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, lead, writable=True),
        {
            "a": make_agent(tmp_path, p1),
            "b": make_agent(tmp_path, p2),
        },
    )
    result = team.run(
        "task",
        (
            SubagentAssignment("a", "role", "同一个只读分析"),
            SubagentAssignment("b", "role", "同一个只读分析"),
        ),
    )
    assert p1.messages and not p2.messages
    blocked = result.dispatch["blocked_assignments"]
    assert blocked == [{"name": "b", "duplicate_of": "a", "work_key": blocked[0]["work_key"]}]
    assert [item.name for item in result.subagents] == ["a"]
    assert result.dispatch["duplicate_assignments_blocked"] == 1
    assert result.usage["total_tokens"] == 30


def test_independent_check_policy_allows_intentional_redundancy(tmp_path):
    """independent_check 允许重复运行，但必须显式记为有意冗余。"""
    p1 = RecordingProvider("结论一")
    p2 = RecordingProvider("结论二")
    lead = RecordingProvider("完成")
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, lead, writable=True),
        {
            "a": make_agent(tmp_path, p1),
            "b": make_agent(tmp_path, p2),
        },
    )
    result = team.run(
        "task",
        (
            SubagentAssignment("a", "role", "同一只读分析"),
            SubagentAssignment("b", "role", "同一只读分析", redundancy_policy="independent_check"),
        ),
    )
    assert p1.messages and p2.messages
    assert [item.name for item in result.subagents] == ["a", "b"]
    intentional = result.dispatch["intentional_redundancy"]
    assert intentional == [
        {"name": "b", "duplicate_of": "a", "work_key": intentional[0]["work_key"]}
    ]
    assert result.dispatch["duplicate_assignments_blocked"] == 0
    assert result.dispatch["intentional_redundancy_runs"] == 1


def test_team_finding_dedup_collapses_identical_conclusions(tmp_path):
    """交接阶段折叠完全相同的结论，只保留一条，并报告折叠数量。"""
    lead = RecordingProvider("完成")
    team = ReadOnlySubagentTeam(
        make_agent(tmp_path, lead, writable=True),
        {
            "a": make_agent(tmp_path, RecordingProvider("相同结论")),
            "b": make_agent(tmp_path, RecordingProvider("相同结论  ")),
        },
    )
    result = team.run(
        "task",
        (
            SubagentAssignment("a", "role", "task-a"),
            SubagentAssignment("b", "role", "task-b"),
        ),
    )
    assert result.dispatch["duplicate_findings_collapsed"] == 1
    assert result.dispatch["handoff_artifact_count"] == 1
    assert "相同结论" in lead.messages[0][-1]["content"]


def test_team_rejects_unknown_redundancy_policy(tmp_path):
    """未知策略必须 fail closed，不能默默当成 unique 或 independent_check。"""
    with pytest.raises(ValueError, match="redundancy_policy"):
        SubagentAssignment("a", "role", "task", redundancy_policy="whatever")


def test_team_emits_dispatch_and_handoff_trace_events(tmp_path):
    """派发、拦截、发现折叠与交接必须在 leader Trace 上留下可审计事件。"""
    from evaluation.report import read_trace
    from persistence.trace import TraceWriter

    trace_path = tmp_path / "trace-leader.jsonl"
    lead = make_agent(tmp_path, RecordingProvider("完成"), writable=True)
    lead.trace = TraceWriter(trace_path)
    team = ReadOnlySubagentTeam(
        lead,
        {
            "a": make_agent(tmp_path, RecordingProvider("相同结论")),
            "b": make_agent(tmp_path, RecordingProvider("相同结论")),
            "c": make_agent(tmp_path, RecordingProvider("相同结论")),
        },
    )
    team.run(
        "task",
        (
            SubagentAssignment("a", "role", "same task"),
            SubagentAssignment("b", "role", "same task"),
            SubagentAssignment("c", "role", "same task", redundancy_policy="independent_check"),
        ),
    )
    events, _issues = read_trace(trace_path)
    names = [row["event"] for row in events]
    assert "subagent_scheduled" in names
    assert "subagent_completed" in names
    assert "duplicate_assignment_blocked" in names
    assert "intentional_redundancy_allowed" in names
    assert "duplicate_finding_collapsed" in names
    assert "subagent_handoff_completed" in names
