"""路由行为使用固定模型替身；验证费用、升级、预算与真实工具边界。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from config import ModelProfile, RoutingSettings, Settings
from core.agent import Agent
from core.schemas import ModelReply, RunStatus, ToolCall
from core.usage import TokenUsage
from providers import ScriptedProvider
from providers.events import ProviderEvent
from routing import RoutedProvider
from routing.intent import classify
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def settings(**rules):
    """价格仅为算术夹具，不代表任何供应商报价。"""
    return Settings(
        {
            "economy": ModelProfile(
                "economy", "https://small.invalid/v1", "small", "fake-small", Decimal(1), Decimal(2)
            ),
            "primary": ModelProfile(
                "primary",
                "https://large.invalid/v1",
                "large",
                "fake-large",
                Decimal(5),
                Decimal(10),
            ),
        },
        RoutingSettings(**rules),
    )


def reply(text="done", call=None):
    return ModelReply(
        final_text=text if call is None else None, tool_call=call, usage=TokenUsage(100, 20, 120)
    )


def run_routed(workspace, small, large, task="列出项目文件", config=None, **kwargs):
    providers = {
        name: ScriptedProvider(replies)
        for name, replies in (("economy", small), ("primary", large))
        if replies
    }
    events = []
    router = RoutedProvider(config or settings(), factory=lambda p: providers[p.name], **kwargs)
    agent = Agent(
        router,
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=True, allow_commands=True),
            approval_callback=lambda *a: True,
        ),
        on_event=events.append,
    )
    result = agent.run(task)
    return result, agent, events


@pytest.mark.parametrize(
    "task,simple",
    [
        ("列出项目文件", True),
        ("读取README.md", True),
        ("list files", True),
        ("find config.py", True),
        ("修复 calculator.py 并运行测试", False),
        ("读取并修改README", False),
        ("设计新的架构", False),
        ("继续", False),
        ("list files and delete them", False),
        ("read file\nrun tests", False),
    ],
)
def test_intent_rules(task, simple):
    assert classify(task)[0] is simple


def test_simple_task_routes_cheap_and_records_real_usage(workspace):
    result, _, events = run_routed(workspace, [reply()], [])
    assert result.ok and result.usage["total_tokens"] == 120
    assert result.routing["known_cost"] == "0.00014"
    assert result.routing["selected_profile"] == "economy"
    assert any(e.type == "model_routed" for e in events)


def test_complex_or_pinned_task_uses_selected_model(workspace):
    complex_result, _, _ = run_routed(workspace, [], [reply()], task="重构项目")
    pinned, _, _ = run_routed(workspace, [], [reply()], mode="primary")
    assert complex_result.ok and pinned.ok
    assert complex_result.routing["known_cost"] == "0.0007"
    assert pinned.routing["cheap_requests"] == 0


def test_cheap_write_is_discarded_before_tool_execution_and_usage_is_counted(workspace):
    write = ToolCall("unsafe", "write_or_replace", {"path": "new.txt", "old": "", "new": "bad"})
    result, agent, _ = run_routed(workspace, [reply(call=write)], [reply()])
    assert result.ok and not (workspace / "new.txt").exists()
    assert not any(m["role"] == "tool" for m in agent.last_messages)
    assert result.routing["escalations"] == 1 and result.usage["requests"] == 2
    assert result.routing["known_cost"] == "0.00084"


def test_real_tool_failure_escalates_and_exposes_result_to_strong(workspace):
    read = ToolCall("missing", "read_file", {"path": "missing.txt"})
    result, agent, _ = run_routed(workspace, [reply(call=read)], [reply()])
    assert result.ok and result.routing["escalations"] == 1
    assert any(m["role"] == "tool" and "false" in m["content"] for m in agent.last_messages)


def test_cheap_round_limit_and_request_budget(workspace):
    read = ToolCall("read", "read_file", {"path": "alpha.txt"})
    result, _, _ = run_routed(
        workspace, [reply(call=read)], [reply()], config=settings(max_cheap_requests=1)
    )
    assert result.ok and result.routing["escalations"] == 1
    stopped, _, _ = run_routed(workspace, [reply(call=read)], [], config=settings(max_requests=1))
    assert stopped.status is RunStatus.PROVIDER_ERROR
    assert stopped.routing["requests"] == 1


def test_cost_threshold_stops_next_request(workspace):
    read = ToolCall("read", "read_file", {"path": "alpha.txt"})
    result, _, _ = run_routed(
        workspace, [reply(call=read)], [], config=settings(max_cost=Decimal("0.0001"))
    )
    assert result.status is RunStatus.PROVIDER_ERROR
    assert result.routing["requests"] == 1 and "费用预算" in result.error


def test_unknown_usage_and_prices_cannot_bypass_cost_budget(workspace):
    read = ModelReply.call(ToolCall("read", "read_file", {"path": "alpha.txt"}))
    result, _, _ = run_routed(workspace, [read], [], config=settings(max_cost=Decimal("1")))
    assert result.status is RunStatus.PROVIDER_ERROR and result.routing["unknown_requests"] == 1
    config = settings(max_cost=Decimal("1"))
    config.profiles["economy"] = replace(config.profiles["economy"], input_per_million=None)
    result, _, _ = run_routed(workspace, [], [], config=config)
    assert result.status is RunStatus.PROVIDER_ERROR and result.routing["requests"] == 0


def test_missing_profile_key_does_not_fallback_or_contact_service(workspace, monkeypatch):
    config = settings()
    config.profiles["economy"] = replace(config.profiles["economy"], api_key="")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key")
    result, _, _ = run_routed(workspace, [], [], config=config)
    assert result.status is RunStatus.PROVIDER_ERROR and result.routing["requests"] == 0
    assert "wrong-key" not in result.error


def test_router_forwards_retry_budget_and_delays_to_http_transport(monkeypatch):
    """防止路由层只接收重试配置，却仍让底层传输使用旧默认值。"""
    captured = {}

    class CapturingProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def events(self, messages, tools, *, stream):
            yield ProviderEvent("message_complete", reply=reply())

    monkeypatch.setattr("routing.router.OpenAICompatibleProvider", CapturingProvider)
    router = RoutedProvider(
        settings(),
        mode="primary",
        max_attempts=3,
        retry_base_delay=2.0,
        max_retry_delay=10.0,
    )

    assert router.complete([{"role": "user", "content": "go"}]).final_text == "done"
    assert captured["max_attempts"] == 3
    assert captured["retry_base_delay"] == 2.0
    assert captured["max_retry_delay"] == 10.0


def test_streaming_routing_filters_tools_and_enforces_policy(workspace):
    observed = []

    class Streaming:
        def events(self, messages, tools, *, stream):
            observed.append([t["function"]["name"] for t in tools])
            yield ProviderEvent("text_delta", {"text": "candidate"})
            yield ProviderEvent(
                "message_complete",
                reply=reply(call=ToolCall("escape", "read_file", {"path": "../outside"})),
            )

    router = RoutedProvider(
        settings(),
        factory=lambda p: Streaming() if p.name == "economy" else ScriptedProvider([reply()]),
    )
    agent = Agent(router, ToolExecutor(WorkspacePolicy(workspace)), streaming=True)
    result = agent.run("list files")
    assert result.ok and set(observed[0]) == {"list_files", "read_file", "search_text"}
    assert "策略拒绝" in agent.last_messages[2]["content"]


def test_run_budget_resets_between_user_turns(workspace):
    router = RoutedProvider(settings(max_requests=1), factory=lambda p: ScriptedProvider([reply()]))
    agent = Agent(router, ToolExecutor(WorkspacePolicy(workspace)))
    first, second = agent.run("list files"), agent.run("继续")
    assert first.ok and second.ok
    assert second.routing["requests"] == 1 and second.routing["selected_profile"] == "primary"


def test_retry_under_money_budget_stops_before_reissuing(workspace):
    retry_ran = []

    class Retrying:
        def events(self, *a, **k):
            yield ProviderEvent("llm_retry", {"next_attempt": 2, "delay_seconds": 0})
            retry_ran.append(True)
            yield ProviderEvent("message_complete", reply=reply())

    router = RoutedProvider(settings(max_cost=Decimal("1")), factory=lambda p: Retrying())
    result = Agent(router, ToolExecutor(WorkspacePolicy(workspace))).run("list files")
    assert result.status is RunStatus.PROVIDER_ERROR and not retry_ran
    assert result.routing["unknown_requests"] == 1
