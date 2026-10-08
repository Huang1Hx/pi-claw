"""轻量分类→能力分流的离线验证；只用替身和 HTTP mock，不访问真实模型。"""

import io
import json
import urllib.request
from dataclasses import replace

import pytest

from config import ModelProfile, RoutingSettings, Settings, load_settings
from core.agent import Agent
from core.control import ExecutionControl
from core.schemas import ModelReply, RunStatus, ToolCall
from core.usage import TokenUsage
from providers import ProviderError, ScriptedProvider
from providers.events import ProviderEvent
from routing import RoutedProvider
from routing.classifier import classification_messages, parse_complexity
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def config(**overrides):
    """共享连接、不同模型、不提供价格；显式开启轻量模型分类。"""
    return Settings(
        {
            name: ModelProfile(name, "https://gateway.invalid/v1", model, "fake-shared")
            for name, model in (("economy", "small"), ("primary", "large"))
        },
        RoutingSettings(intent_strategy="model", **overrides),
    )


def classified(complexity="simple"):
    """给分类阶段固定 Token，便于验证它未从总用量中漏记。"""
    return ModelReply(
        final_text=json.dumps({"complexity": complexity}), usage=TokenUsage(50, 5, 55)
    )


def answer(text="done"):
    """执行阶段回复，不含任何未经授权的工具动作。"""
    return ModelReply(final_text=text, usage=TokenUsage(100, 20, 120))


def run(workspace, small, large, *, task="判断这句话属于哪个意图分支", settings=None, mode="auto"):
    """走真实 Agent 和工具边界，只有模型推理替换为脚本响应。"""
    pool = {
        name: ScriptedProvider(replies)
        for name, replies in (("economy", small), ("primary", large))
        if replies
    }
    events = []
    router = RoutedProvider(settings or config(), mode=mode, factory=lambda p: pool[p.name])
    agent = Agent(
        router,
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=True, allow_commands=True),
            approval_callback=lambda *args: True,
        ),
        on_event=events.append,
    )
    return agent.run(task), agent, events


def test_simple_intent_uses_light_model_and_accounts_for_classification(workspace):
    """分类与执行分开记账；简单语义分类不受旧的只读关键词规则限制。"""
    result, agent, events = run(workspace, [classified(), answer()], [])
    assert result.ok and result.routing["selected_profile"] == "economy"
    assert result.routing["requests"] == 2
    assert result.routing["classification_requests"] == result.routing["cheap_requests"] == 1
    assert result.usage["requests"] == 2 and result.usage["total_tokens"] == 175
    assert result.routing["intent"]["source"] == "model"
    assert "known_cost" not in result.routing and "currency" not in result.routing
    assert "unknown_requests" not in result.routing
    assert result.routing["models"]["economy"]["prompt_tokens"] == 150
    assert "complexity" not in json.dumps(agent.last_messages)
    assert any(e.type == "intent_classified" for e in events)
    assert [e.data["purpose"] for e in events if e.type == "model_routed"] == [
        "classification",
        "execution",
    ]


def test_complex_task_is_classified_by_light_then_executed_by_strong(workspace):
    """分类模型不承担复杂任务执行；首次选择主力不是失败升级。"""
    result, _, _ = run(workspace, [classified("complex")], [answer()], task="分析大型模块依赖关系")
    assert result.ok and result.routing["selected_profile"] == "primary"
    assert result.routing["cheap_requests"] == result.routing["escalations"] == 0
    assert result.routing["requests"] == 2 and result.usage["total_tokens"] == 175
    assert set(result.routing["models"]) == {"economy", "primary"}


@pytest.mark.parametrize(
    "task",
    [
        "修改源代码",
        "运行测试",
        "继续",
        "go on",
        "edit app.py",
        "写一个hello world，用python",
        "写个示例脚本",
        "新建 hello.py",
        "保存代码到文件",
        "读取数据并写入新文件",
        "列出文件并写入缓存",
        "创建一个项目",
        "生成 Python 代码",
        "create hello.py",
    ],
)
def test_host_guard_does_not_downgrade_obviously_complex_tasks(workspace, task):
    """即使分类误报 simple，宿主仍对副作用和缺失上下文任务兜底。"""
    result, _, _ = run(workspace, [classified()], [answer()], task=task)
    assert result.ok and result.routing["selected_profile"] == "primary"
    assert result.routing["intent"]["source"] == "host_guard"
    assert result.routing["classification_failures"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "simple",
        "not json",
        '"simple"',
        "[]",
        "{}",
        '{"complexity":true}',
        '{"complexity":"medium"}',
        '{"complexity":"simple","profile":"external"}',
        '{"complexity":"complex","complexity":"simple"}',
        '```json\n{"complexity":"simple"}\n```',
        "x" * 257,
    ],
)
def test_invalid_classifier_response_fails_closed(workspace, text):
    """畸形标签既不控制任意模型，也不泄露到任务消息；已发生的 usage 仍统计。"""
    result, agent, _ = run(workspace, [replace(classified(), final_text=text)], [answer()])
    assert result.ok and result.routing["intent"]["source"] == "fallback"
    assert result.routing["selected_profile"] == "primary"
    assert result.routing["classification_failures"] == 1
    assert result.usage["total_tokens"] == 175
    assert agent.last_messages[-1]["content"] == "done"


def test_classifier_cannot_execute_tool_even_when_user_has_approved_writes(workspace):
    """分类工具候选完全丢弃；不能因测试授权写入而越过分类阶段的零工具权限。"""
    unsafe = ModelReply.call(
        ToolCall("bad", "write_or_replace", {"path": "must-not-exist.txt", "old": "", "new": "bad"})
    )
    result, agent, _ = run(workspace, [unsafe], [answer()])
    assert result.ok and result.routing["classification_failures"] == 1
    assert not (workspace / "must-not-exist.txt").exists()
    assert not any(m["role"] == "tool" for m in agent.last_messages)
    assert result.usage["requests_without_usage"] == 1


def test_classification_failure_falls_back_without_logging_raw_error(workspace):
    """错误正文不进入事件或会话；异常 fallback 同样受总请求预算约束。"""
    calls = []

    def factory(profile):
        """模拟轻量服务异常，主力服务正常。"""
        calls.append(profile.name)
        if profile.name == "economy":
            raise ProviderError("secret-that-must-not-leak")
        return ScriptedProvider([answer()])

    events = []
    router = RoutedProvider(config(), factory=factory)
    agent = Agent(router, ToolExecutor(WorkspacePolicy(workspace)), on_event=events.append)
    result = agent.run("判断意图")
    assert result.ok and calls == ["economy", "primary"]
    assert result.routing["classification_failures"] == 1
    assert result.routing["requests"] == 2 and result.usage["requests"] == 1
    assert "secret-that-must-not-leak" not in repr(events) + repr(result)


def test_classification_cannot_bypass_total_request_limit(workspace):
    """分类也消耗总请求配额，配额耗尽后不得继续主力请求。"""
    result, _, _ = run(workspace, [classified("complex")], [], settings=config(max_requests=1))
    assert result.status is RunStatus.REQUEST_BUDGET_EXHAUSTED
    assert result.routing["requests"] == result.routing["classification_requests"] == 1
    assert result.usage["total_tokens"] == 55


def test_cancellation_during_classification_never_starts_execution_model(workspace):
    """分类结束时已取消，不能被 fallback 吞掉后继续请求主力模型。"""
    control = ExecutionControl()
    calls = []

    class CancellingClassifier:
        def complete(self, messages, tools=None):
            control.cancel()
            return classified("complex")

    def factory(profile):
        calls.append(profile.name)
        return CancellingClassifier() if profile.name == "economy" else ScriptedProvider([answer()])

    router = RoutedProvider(config(), factory=factory)
    result = Agent(router, ToolExecutor(WorkspacePolicy(workspace))).run(
        "判断意图", control=control
    )

    assert result.status is RunStatus.CANCELLED
    assert calls == ["economy"]
    assert result.routing["requests"] == 1


def test_task_length_bound_skips_classifier_and_uses_strong(workspace):
    """超长任务不截断后误判，只跳过分类；执行模型依旧收到原始任务。"""
    result, agent, _ = run(workspace, [], [answer()], task="读" * 4001)
    assert result.ok and result.routing["classification_requests"] == 0
    assert result.routing["classification_failures"] == 1
    assert next(m["content"] for m in agent.last_messages if m["role"] == "user") == "读" * 4001


def test_fixed_profile_skips_classification(workspace):
    """用户固定模型时，不暗中增加分类调用或强制换模型。"""
    result, _, _ = run(workspace, [], [answer()], mode="primary")
    assert result.ok and result.routing["classification_requests"] == 0
    assert result.routing["requests"] == 1 and result.routing["intent"] is None


def test_same_profile_for_both_roles_skips_classification(workspace):
    """没有两个不同角色时无需花一次请求判断。"""
    result, _, _ = run(workspace, [], [answer()], settings=config(cheap="primary"))
    assert result.ok and result.routing["classification_requests"] == 0


def test_classify_once_per_turn_and_do_not_spend_execution_quota(workspace):
    """工具反馈不会重复触发分类；下一用户回合重新分类、重置计数。"""
    read = ModelReply.call(ToolCall("read", "read_file", {"path": "alpha.txt"}))
    pool = ScriptedProvider([classified(), read, answer(), classified(), answer()])
    router = RoutedProvider(config(), factory=lambda p: pool)
    agent = Agent(router, ToolExecutor(WorkspacePolicy(workspace)))
    first = agent.run("读取alpha.txt")
    second = agent.run("意图识别")
    assert first.ok and second.ok
    assert first.routing["requests"] == 3 and first.routing["cheap_requests"] == 2
    assert first.routing["classification_requests"] == 1 and first.routing["escalations"] == 0
    assert second.routing["requests"] == 2 and second.routing["classification_requests"] == 1


def test_classifier_deltas_are_private_and_stream_is_closed(workspace):
    """即使注入 Provider 违规输出分类 delta，也不展示或写入会话。"""
    closed = []

    class Noisy:
        """异常流模拟器：完整响应之后多发事件，宿主必须拒绝。"""

        def events(self, messages, tools, *, stream):
            """分类只接收当前任务，无工具且不要求流式。"""
            assert tools == [] and stream is False
            assert messages == classification_messages("判断意图")
            try:
                yield ProviderEvent("text_delta", {"text": "private-classification"})
                yield ProviderEvent("message_complete", reply=classified())
                yield ProviderEvent("text_delta", {"text": "invalid-tail"})
            finally:
                closed.append(True)

    router = RoutedProvider(
        config(), factory=lambda p: Noisy() if p.name == "economy" else ScriptedProvider([answer()])
    )
    events = []
    agent = Agent(router, ToolExecutor(WorkspacePolicy(workspace)), on_event=events.append)
    result = agent.run("判断意图")
    assert result.ok and result.routing["classification_failures"] == 1 and closed == [True]
    assert not any(e.type == "text_delta" for e in events)


@pytest.mark.parametrize("streaming", [False, True])
def test_shared_connection_changes_only_model_role_with_bounded_classifier(
    workspace, monkeypatch, streaming
):
    """经过真实 HTTP 编码验证 Key/URL 复用，分类无工具、短输出，任务照常支持 SSE。"""
    requests = []

    def response(request, **kwargs):
        """只替换 HTTP 收发，未绕过模型请求编码或响应解析。"""
        requests.append(request)
        payload = json.loads(request.data)
        text = '{"complexity":"complex"}' if len(requests) == 1 else "done"
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        if payload.get("stream"):
            chunk = {
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
                "usage": usage,
            }
            return io.BytesIO(b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n")
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": usage,
                }
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", response)
    router = RoutedProvider(config())
    assert not requests
    result = Agent(router, ToolExecutor(WorkspacePolicy(workspace)), streaming=streaming).run(
        "复杂任务"
    )
    assert result.ok and len(requests) == 2
    assert {r.full_url for r in requests} == {"https://gateway.invalid/v1/chat/completions"}
    assert {r.headers["Authorization"] for r in requests} == {"Bearer fake-shared"}
    first, second = [json.loads(r.data) for r in requests]
    assert [first["model"], second["model"]] == ["small", "large"]
    assert "tools" not in first and "tool_choice" not in first
    assert not first.get("stream") and bool(second.get("stream")) is streaming
    assert first["max_tokens"] == 128 and second["max_tokens"] == 2048
    assert len(second["tools"]) == 6
    assert result.usage["total_tokens"] == 240


@pytest.mark.parametrize("value", ['"invalid"', '["model"]', "true", "42"])
def test_invalid_intent_strategy_rejected_before_request(tmp_path, value):
    """配置入口拒绝未知策略和非字符串类型，不留到运行时失败。"""
    path = tmp_path / "config.toml"
    path.write_text(
        '[connection]\nbase_url="https://example.invalid/v1"\napi_key_env="KEY"\n'
        '[models.economy]\nmodel="small"\n[models.primary]\nmodel="large"\n'
        f"[routing]\nintent_strategy={value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="intent_strategy"):
        load_settings(config_path=path, environ={"KEY": "fake"}, no_env=True)


def test_parser_rejects_nontext_and_limits_classification_input():
    """协议验证不能只依赖供应商按预期返回字段。"""
    with pytest.raises(ValueError):
        parse_complexity(ModelReply())
    with pytest.raises(ValueError):
        classification_messages("x" * 4001)
