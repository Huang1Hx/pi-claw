"""运行时新能力的行为验收：Hooks、重复调用、裁剪、事件与持久用量。"""

import copy
import json

import pytest

from core.agent import Agent
from core.context import ContextManager
from core.hooks import HookBus, HookDecision
from core.schemas import ModelReply, RunStatus, ToolCall
from core.usage import TokenUsage, UsageTracker
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def test_loop_detector_stops_cycles_and_preserves_protocol(workspace):
    calls = [
        ModelReply.call(
            ToolCall(str(i), "read_file", {"path": "alpha.txt" if i % 2 == 0 else "beta.txt"})
        )
        for i in range(8)
    ]
    events = []
    agent = Agent(
        ScriptedProvider(calls),
        ToolExecutor(WorkspacePolicy(workspace)),
        max_steps=8,
        on_event=events.append,
    )
    result = agent.run("inspect forever")
    assert result.status is RunStatus.LOOP_DETECTED and result.steps == 5
    ContextManager._validate_tool_protocol(agent.last_messages)
    assert any(e.type == "loop_detected" for e in events)


def test_hook_blocks_write_and_is_observed(workspace):
    hooks = HookBus()
    seen = []
    hooks.register("before_tool", lambda _: HookDecision(False, "read only"))
    hooks.register("after_tool", lambda result: seen.append(result))
    hooks.register("run_end", lambda result: seen.append(result))
    call = ToolCall("write", "write_or_replace", {"path": "new.txt", "old": "", "new": "content"})
    result = Agent(
        ScriptedProvider([ModelReply.call(call), ModelReply.final("blocked")]),
        ToolExecutor(WorkspacePolicy(workspace), approval_callback=lambda *a: True),
        hooks=hooks,
    ).run("write")
    assert result.ok
    assert not (workspace / "new.txt").exists()
    assert "Hook 拒绝" in seen[0]["error"]
    assert seen[-1]["status"] == "completed"


def test_hook_cannot_mutate_call_or_override_hard_deny(workspace):
    hooks = HookBus()

    def change(payload):
        payload["arguments"]["path"] = "alpha.txt"
        return HookDecision()

    hooks.register("before_tool", change)
    call = ToolCall("read", "read_file", {"path": "../secret.txt"})
    agent = Agent(
        ScriptedProvider([ModelReply.call(call), ModelReply.final("denied")]),
        ToolExecutor(WorkspacePolicy(workspace), approval_callback=lambda *a: True),
        hooks=hooks,
    )
    agent.run("read")
    result = json.loads(agent.last_messages[-2]["content"])
    assert not result["ok"] and "策略拒绝" in result["error"]
    assert call.arguments["path"] == "../secret.txt"


def test_pre_hook_exception_fails_closed(workspace):
    hooks = HookBus()

    def broken(_):
        raise RuntimeError("plugin error")

    hooks.register("before_tool", broken)
    agent = Agent(
        ScriptedProvider(
            [
                ModelReply.call(ToolCall("a", "read_file", {"path": "alpha.txt"})),
                ModelReply.final("blocked"),
            ]
        ),
        ToolExecutor(WorkspacePolicy(workspace)),
        hooks=hooks,
    )
    agent.run("read")
    assert "前置 Hook 失败" in agent.last_messages[-2]["content"]


def test_observer_exception_does_not_repeat_tool(workspace):
    count = []

    def observer(_):
        count.append(1)
        raise RuntimeError("display broken")

    agent = Agent(
        ScriptedProvider(
            [
                ModelReply.call(ToolCall("a", "read_file", {"path": "alpha.txt"})),
                ModelReply.final("done"),
            ]
        ),
        ToolExecutor(WorkspacePolicy(workspace)),
        on_event=observer,
    )
    assert agent.run("read").ok
    assert sum(m["role"] == "tool" for m in agent.last_messages) == 1
    assert count


def history_with_large_tool():
    """保留实际 tool call 配对，避免只测试任意文本截断。"""
    return [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "old task"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "a", "function": {"name": "run_command", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "a",
            "content": json.dumps(
                {
                    "ok": False,
                    "error": "test failed",
                    "output": {"returncode": 1, "stdout": "x" * 12000},
                }
            ),
        },
        {"role": "assistant", "content": "old final"},
        {"role": "user", "content": "new task"},
    ]


def test_pruning_preserves_original_and_error_evidence():
    manager = ContextManager(keep_recent=2, prune_tool_chars=256)
    original = history_with_large_tool()
    saved = copy.deepcopy(original)
    pruned, count = manager.prune_tool_results(original)
    assert count == 1 and original == saved
    result = json.loads(pruned[3]["content"])
    assert result["context_pruned"] and result["returncode"] == 1
    assert result["error"] == "test failed" and result["ok"] is False
    assert len(pruned[3]["content"]) < 1000
    ContextManager._validate_tool_protocol(pruned)
    assert manager.prune_tool_results(pruned)[1] == 0
    assert manager.prune_tool_results(original[:5])[1] == 0


def test_pruning_keeps_durable_journal(tmp_path, workspace):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "rules")
    for message in history_with_large_tool()[1:]:
        store.append_message(session.session_id, workspace, message)
    original = store.load_messages(session.session_id, workspace)
    pruned, count = ContextManager(keep_recent=2, prune_tool_chars=256).prune_tool_results(original)
    store.save_active_messages(session.session_id, workspace, pruned)
    assert count == 1
    assert store.load_messages(session.session_id, workspace) == original
    assert store.load_active_messages(session.session_id, workspace) == pruned


def test_usage_is_idempotent_and_survives_resume(tmp_path, workspace):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    tracker = UsageTracker()
    tracker.add(TokenUsage(10, 5, 15))
    tracker.add(None)
    for _ in range(2):
        store.record_usage(session.session_id, workspace, "run-1", tracker.to_dict())
    resumed = SessionStore(store.data_dir)
    totals = resumed.total_usage(session.session_id, workspace)
    assert totals["total_tokens"] == 15 and totals["requests"] == 2
    assert totals["requests_without_usage"] == 1
    session_dir = store._session_dir(session.session_id, store._workspace_root(workspace))
    assert (session_dir / "usage.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": -1, "completion_tokens": 0},
        {"prompt_tokens": True, "completion_tokens": 0},
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 1},
    ],
)
def test_invalid_usage_rejected(usage):
    with pytest.raises(ValueError):
        TokenUsage.from_dict(usage)


def test_repl_usage_command_after_offline_turn(tmp_path, workspace):
    import io

    output = io.StringIO()
    repl = CodingRepl(
        workspace=workspace,
        provider=ScriptedProvider([ModelReply.final("done")]),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=output,
    )
    assert repl.run_turn("hello") == 0
    assert repl._handle_command("/usage")
    assert "usage 未知的响应：1" in output.getvalue()
