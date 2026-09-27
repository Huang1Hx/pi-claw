"""按任务复杂度分配轻量/主力模型；分类与执行隔离，工具权限始终由宿主控制。"""

import json
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import replace
from typing import Any

from config.models import ModelProfile, Settings
from core.schemas import ModelReply, RunResult, ToolCall, ToolResult
from providers import OpenAICompatibleProvider, ProviderError
from providers.events import ProviderEvent
from routing.classifier import classification_messages, parse_complexity, require_strong
from routing.cost import CostLedger
from routing.intent import READ_TOOLS, classify
from routing.workflow import WorkflowCoordinator


class RoutedProvider:
    """路由只改模型选择，所有工具仍由统一 Policy/Approval 授权。"""

    def __init__(
        self,
        settings: Settings,
        *,
        mode: str = "auto",
        max_attempts: int = 3,
        factory: Callable[[ModelProfile], Any] | None = None,
    ) -> None:
        """校验固定/自动模式与模型配置，注入测试 Provider 工厂；此处不发起请求。"""
        if mode != "auto" and mode not in settings.profiles:
            raise ValueError("未知模型 profile")
        if not settings.profiles:
            raise ValueError("路由需要 --config 指定模型配置")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1–10 的整数")
        if not isinstance(
            settings.routing.intent_strategy, str
        ) or settings.routing.intent_strategy not in {"rules", "model"}:
            raise ValueError("intent_strategy 必须是 rules 或 model")
        if mode == "auto" and any(
            name not in settings.profiles
            for name in (settings.routing.cheap, settings.routing.strong)
        ):
            raise ValueError("自动路由引用的模型 profile 不存在")
        if mode == "auto" and settings.workflow.enabled:
            roles = [settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer]
            if settings.workflow.premium_review_policy != "off":
                roles.append(settings.workflow.premium_reviewer)
            if any(name not in settings.profiles for name in roles):
                raise ValueError("工作流角色引用的模型 profile 不存在")
        self.settings, self.mode = settings, mode
        self.factory, self.max_attempts = factory, max_attempts
        self.begin_run("")

    def begin_run(self, task: str) -> None:
        """每个用户回合重新建立预算；分类延迟到首个请求，升级在本回合内保持。"""
        rules = self.settings.routing
        self.task = task
        simple, self.reason = classify(task)
        self.current = (
            (rules.cheap if simple else rules.strong) if self.mode == "auto" else self.mode
        )
        if self.mode != "auto":
            self.reason = "用户固定模型"
        self._intent_pending = (
            self.mode == "auto" and rules.intent_strategy == "model" and rules.cheap != rules.strong
        )
        self.intent: dict[str, Any] | None = None
        self.requests = 0
        self.cheap_requests = 0
        self.classification_requests = 0
        self.classification_failures = 0
        self.escalations = 0
        self.requests_by_purpose: dict[str, int] = {}
        self.workflow: WorkflowCoordinator | None = None
        # 未提供价格时只统计请求与 Token；历史显式价格/金额预算仍兼容，但不参与选模。
        priced = rules.max_cost is not None or any(
            p.input_per_million is not None or p.output_per_million is not None
            for p in self.settings.profiles.values()
        )
        self.cost = CostLedger(rules.currency, enabled=priced)

    def summary(self) -> dict[str, Any]:
        """返回无密钥的路由结果；分类请求属于总请求，但不占轻量执行请求配额。"""
        return {
            "mode": self.mode,
            "selected_profile": self.current,
            "requests": self.requests,
            "cheap_requests": self.cheap_requests,
            "classification_requests": self.classification_requests,
            "classification_failures": self.classification_failures,
            "intent": dict(self.intent) if self.intent is not None else None,
            "escalations": self.escalations,
            "requests_by_purpose": dict(self.requests_by_purpose),
            "workflow": self.workflow.summary() if self.workflow is not None else None,
            **self.cost.to_dict(),
        }

    def _escalate(self, reason: str) -> ProviderEvent:
        """将当前回合固定到主力 profile，累计升级次数并返回可审计的原因事件。"""
        self.current, self.reason = self.settings.routing.strong, reason
        self.escalations += 1
        return ProviderEvent("route_escalated", {"profile": self.current, "reason": reason})

    def observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        """将真实工具结果交给当前工作流；普通两档路由和固定模型无需阶段状态。"""
        if self.workflow is not None:
            self.workflow.observe_tool(call, result)

    def finish_run(self, result: RunResult) -> None:
        """外层 Agent 的最终验收优先于模型审查结论。"""
        if self.workflow is not None:
            self.workflow.finish_run(result)

    def _needs_upgrade(self, messages: Sequence[dict]) -> str | None:
        """依据轻量执行请求配额、宿主拒绝或最新工具结果决定是否升级。"""
        if self.cheap_requests >= self.settings.routing.max_cheap_requests:
            return "轻量模型执行请求配额耗尽"
        if messages and messages[-1].get("role") == "user":
            try:
                feedback = json.loads(messages[-1].get("content", ""))
            except (TypeError, ValueError):
                feedback = None
            if isinstance(feedback, dict) and feedback.get("type") == "host_completion_rejected":
                return "宿主完成验收拒绝"
        # 只读取最近一组真实工具结果，不把更早回合的失败带进本回合。
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            try:
                result = json.loads(message.get("content", "{}"))
            except (TypeError, ValueError):
                return "工具反馈格式异常"
            if not isinstance(result, dict) or result.get("ok") is not True:
                return "工具失败或授权被拒绝"
        return None

    def _request(
        self,
        profile_name: str,
        messages: Sequence[dict],
        tools: Sequence[dict],
        *,
        purpose: str,
        stream: bool = False,
    ) -> Generator[ProviderEvent, None, ModelReply]:
        """统一分类/执行的预算与用量入口；完整消费底层流，分类原文不向用户透传。"""
        rules = self.settings.routing
        if self.requests >= rules.max_requests:
            raise ProviderError("已达到本回合模型请求上限")
        # 仅兼容用户主动配置的旧金额预算；默认配置不需要单价，也不计算费用。
        if rules.max_cost is not None and (
            self.cost.known_cost >= rules.max_cost or self.cost.unknown_requests
        ):
            raise ProviderError("费用预算已达上限或费用未知，停止下一次请求")
        profile = self.settings.profiles[profile_name]
        if not profile.api_key or any(c.isspace() for c in profile.api_key):
            raise ProviderError(f"profile {profile.name} 缺少有效密钥，请填写对应环境变量")
        if rules.max_cost is not None and (
            profile.input_per_million is None or profile.output_per_million is None
        ):
            raise ProviderError("设置费用预算时必须配置该模型的输入/输出单价")
        is_classification = purpose == "classification"
        self.requests += 1
        self.requests_by_purpose[purpose] = self.requests_by_purpose.get(purpose, 0) + 1
        self.classification_requests += int(is_classification)
        self.cheap_requests += int(
            not is_classification
            and self.mode == "auto"
            and profile_name == rules.cheap
            and profile_name != rules.strong
        )
        if purpose in {"planning", "coding", "reviewing", "premium_reviewing"}:
            self.current = profile_name
        yield ProviderEvent(
            "model_routed",
            {
                "profile": profile.name,
                "model": profile.model,
                "purpose": purpose,
                "reason": "轻量模型判断任务复杂度" if is_classification else self.reason,
                "request": self.requests,
            },
        )
        events = None
        reply = None
        try:
            # 连接来自已解析的 profile；普通角色可共享连接，高级审查可使用独立供应商。
            provider = (
                self.factory(profile)
                if self.factory is not None
                else OpenAICompatibleProvider(
                    api_key=profile.api_key,
                    base_url=profile.base_url,
                    model=profile.model,
                    max_attempts=profile.max_attempts or self.max_attempts,
                    max_output_tokens=min(128, profile.max_output_tokens or rules.max_output_tokens)
                    if is_classification
                    else profile.max_output_tokens or rules.max_output_tokens,
                    thinking_mode=profile.thinking_mode,
                    thinking_format=profile.thinking_format,
                    profile_name=profile.name,
                    timeout=profile.timeout_seconds,
                )
            )
            events_method = getattr(provider, "events", None)
            if callable(events_method):
                events = events_method(messages, tools, stream=stream)
                for event in events:
                    if reply is not None:
                        raise ProviderError("完整模型消息后仍有事件")
                    if event.type == "message_complete":
                        reply = event.reply
                    elif event.type == "llm_retry":
                        if rules.max_cost is not None:
                            raise ProviderError("失败请求费用未知，已阻止预算模式下的自动重试")
                        self.cost.unknown_requests += 1
                        yield event
                    elif purpose not in {
                        "classification",
                        "planning",
                        "reviewing",
                        "premium_reviewing",
                    }:
                        yield event
            else:
                reply = provider.complete(messages, tools)
            if not isinstance(reply, ModelReply):
                raise ProviderError("路由 Provider 没有返回完整消息")
        except Exception:
            self.cost.unknown_requests += 1
            raise
        finally:
            # 上层遇错或关闭流时也关闭 HTTP 生成器，避免分类失败留下未消费连接。
            close = getattr(events, "close", None)
            if callable(close):
                close()
        self.cost.add(profile, reply.usage)
        return replace(reply, tool_call=None, source_profile=profile_name)

    def _classify_task(self) -> Iterator[ProviderEvent]:
        """每回合只分类一次；非法输出、网络错误或过长任务都保守转主力，不执行候选工具。"""
        self._intent_pending = False
        complexity, source = "complex", "fallback"
        reason = "轻量分类失败或结果无效，使用主力模型"
        try:
            messages = classification_messages(self.task)
            reply = yield from self._request(
                self.settings.routing.cheap, messages, [], purpose="classification"
            )
            # 分类不进入会话消息历史，但已经发生的 Token 必须计入 Agent 总用量。
            yield ProviderEvent(
                "usage_observed", {"usage": reply.usage.to_dict() if reply.usage else None}
            )
            complexity, source = parse_complexity(reply), "model"
            if complexity == "simple" and require_strong(self.task):
                complexity, source = "complex", "host_guard"
            reason = "轻量模型判定简单任务" if complexity == "simple" else "复杂任务使用主力模型"
        except Exception:
            self.classification_failures += 1
            complexity, source = "complex", "fallback"
        self.current = (
            self.settings.routing.cheap if complexity == "simple" else self.settings.routing.strong
        )
        self.reason = reason
        self.intent = {
            "strategy": "model",
            "source": source,
            "complexity": complexity,
            "profile": self.current,
            "reason": reason,
        }
        yield ProviderEvent("intent_classified", dict(self.intent))

    def complete(self, messages: Sequence[dict], tools: Sequence[dict] | None = None) -> ModelReply:
        """兼容同步 Provider 协议；底层响应已完整校验才交付。"""
        for event in self.events(messages, tools):
            if event.type == "message_complete":
                return event.reply
        raise ProviderError("路由没有返回完整消息")

    def events(
        self, messages: Sequence[dict], tools: Sequence[dict] | None = None, *, stream: bool = False
    ) -> Iterator[ProviderEvent]:
        """先分类再执行；轻量执行仅暴露只读工具，越界候选丢弃并升级，原权限链不变。"""
        if self._intent_pending:
            yield from self._classify_task()
        cheap = (
            self.mode == "auto"
            and self.current == self.settings.routing.cheap
            and self.current != self.settings.routing.strong
        )
        if cheap and (reason := self._needs_upgrade(messages)):
            yield self._escalate(reason)
        while True:
            if (
                self.mode == "auto"
                and self.settings.workflow.enabled
                and (self.workflow is not None or self.current == self.settings.routing.strong)
            ):
                if self.workflow is None:
                    self.workflow = WorkflowCoordinator(
                        self.settings.workflow, self.task, self._request
                    )
                yield from self.workflow.events(messages, tools or [], stream=stream)
                return
            cheap = (
                self.mode == "auto"
                and self.current == self.settings.routing.cheap
                and self.current != self.settings.routing.strong
            )
            # Schema 只是第一道约束；还必须检查完整回复，不能信任模型一定遵守工具列表。
            selected_tools = [
                t for t in tools or [] if not cheap or t["function"]["name"] in READ_TOOLS
            ]
            reply = yield from self._request(
                self.current, messages, selected_tools, purpose="execution", stream=stream
            )
            if cheap and any(call.name not in READ_TOOLS for call in reply.tool_calls):
                yield ProviderEvent(
                    "usage_observed", {"usage": reply.usage.to_dict() if reply.usage else None}
                )
                yield self._escalate("轻量模型请求了副作用或未知工具")
                continue
            yield ProviderEvent("message_complete", reply=reply)
            return
