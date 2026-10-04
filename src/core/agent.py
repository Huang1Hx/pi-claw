"""有最大步数、可审计的单 Agent 执行循环。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.completion import CompletionCheck, CompletionVerifier, ToolObservation
from core.context import ContextManager
from core.control import ExecutionCancelled, ExecutionControl, ExecutionDeadlineExceeded
from core.events import AgentEvent
from core.hooks import HookBus, HookDecision
from core.loop_detection import RepetitionGuard
from core.schemas import ModelReply, RunResult, RunStatus, ToolCall, ToolResult
from core.usage import TokenUsage, UsageTracker
from persistence.trace import TraceWriter, redact
from providers import Provider, ProviderError, RequestBudgetExceeded, WorkflowStopped
from tools import ApprovalCallback, ToolExecutor

# 逐条消息持久化回调，与只负责展示进度的 on_event 分开；存储失败不能静默忽略。
MessageCallback = Callable[[dict[str, Any]], None]


class Agent:
    """模型决策与工具执行编排；界面通过事件订阅获取实时进展。"""

    def __init__(
        self,
        provider: Provider,
        tool_executor: ToolExecutor,
        *,
        approval_callback: ApprovalCallback | None = None,
        trace: TraceWriter | str | Path | None = None,
        max_steps: int = 8,
        max_tool_calls_per_reply: int = 16,
        max_tool_call_chars: int = 100_000,
        system_prompt: str | None = None,
        context_manager: ContextManager | None = None,
        on_message: MessageCallback | None = None,
        completion_verifier: CompletionVerifier | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        hooks: HookBus | None = None,
        streaming: bool = False,
        repetition_threshold: int = 3,
    ) -> None:
        """装配模型、工具和回调，校验每回合预算；构造时不请求模型或执行工具。"""
        if max_steps <= 0 or max_tool_calls_per_reply <= 0 or max_tool_call_chars <= 0:
            raise ValueError(
                "max_steps、max_tool_calls_per_reply 与 max_tool_call_chars 必须大于 0"
            )
        self.provider = provider
        self.tool_executor = tool_executor
        if approval_callback is not None:
            self.tool_executor.approval_callback = approval_callback
        self.trace = TraceWriter(trace) if isinstance(trace, (str, Path)) else trace
        self.max_steps = max_steps
        self.max_tool_calls_per_reply = max_tool_calls_per_reply
        self.max_tool_call_chars = max_tool_call_chars
        self.system_prompt = system_prompt
        self.context_manager = context_manager
        self.on_message = on_message
        self.completion_verifier = completion_verifier
        self.last_messages: list[dict[str, Any]] = []
        self.on_event = on_event
        self.hooks = hooks or HookBus()
        self.streaming = streaming
        RepetitionGuard(repetition_threshold)
        self.repetition_threshold = repetition_threshold
        self.usage = UsageTracker()
        self._event_seq = 0
        self._running = False
        self._workflow_generation = 0
        self._control: ExecutionControl | None = None

    def run(
        self,
        task: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        control: ExecutionControl | None = None,
    ) -> RunResult:
        """执行一个用户回合，返回包含终态、用量和路由摘要的 RunResult。

        task 必须非空；messages 若传入，将被原地追加/压缩，供下一回合复用。
        每回合重新建立步数、用量和路由预算；同一实例不可并发复用。
        模型错误、预算耗尽等以 RunResult 表达；存储等未捕获异常仍会向上传播。
        """
        # 这是重入保护标记，不是线程互斥锁；并发任务必须创建独立 Agent。
        if self._running:
            raise RuntimeError("同一个 Agent 不支持重入；并发任务应创建独立实例")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task 必须是非空字符串")
        self._running = True
        run_id = uuid.uuid4().hex
        self.usage = UsageTracker()
        self._event_seq = 0
        self._workflow_generation = 0
        self._control = control or ExecutionControl()
        result = None
        try:
            set_control = getattr(self.provider, "set_control", None)
            if callable(set_control):
                set_control(self._control)
            begin_run = getattr(self.provider, "begin_run", None)
            if callable(begin_run):
                begin_run(task)
            self._fire_hook("run_start", {"task": task}, 0, run_id)
            result = self._run(task, messages=messages, run_id=run_id)
            finish_run = getattr(self.provider, "finish_run", None)
            if callable(finish_run):
                finish_run(result)
            summary = getattr(self.provider, "summary", None)
            routing = summary() if callable(summary) else None
            if routing is not None:
                self._record("routing_summary", routing, result.steps, run_id)
            return replace(result, usage=self.usage.to_dict(), routing=routing)
        finally:
            payload = {
                "status": result.status.value if result else "interrupted",
                "usage": self.usage.to_dict(),
            }
            self._fire_hook("run_end", payload, result.steps if result else 0, run_id)
            self._emit("run_end", payload, result.steps if result else 0, run_id)
            self._running = False
            self._control = None

    def _run(
        self,
        task: str,
        *,
        messages: list[dict[str, Any]] | None,
        run_id: str,
    ) -> RunResult:
        """在给定会话历史上完成一个用户回合。

        v0 调用方不传 ``messages`` 时仍得到一次性运行；交互式 Coding Agent
        传入可变列表，使后续用户输入能继续使用同一上下文。
        """

        if not isinstance(task, str) or not task.strip():
            raise ValueError("task 必须是非空字符串")
        history = messages if messages is not None else []
        if not history and self.system_prompt:
            self._append_message(history, {"role": "system", "content": self.system_prompt})
        self._append_message(history, {"role": "user", "content": task})
        self.last_messages = history
        self._record("run_started", {"task": task, "max_steps": self.max_steps}, 0, run_id)
        observations: list[ToolObservation] = []
        guard = RepetitionGuard(self.repetition_threshold)
        guard_generation = self._workflow_generation

        for step in range(1, self.max_steps + 1):
            try:
                if self._control is not None:
                    self._control.checkpoint()
                # 只缩减发给模型的活动窗口，已落盘的原始 journal/trace 不随之删除。
                if self.context_manager:
                    pruned, count = self.context_manager.prune_tool_results(history)
                    if count:
                        history[:] = pruned
                        self._record("context_pruned", {"tool_results": count}, step, run_id)
                if self.context_manager and self.context_manager.needs_compaction(history):
                    self._fire_hook("before_compact", {"messages": len(history)}, step, run_id)
                    before = len(history)
                    history[:] = self.context_manager.compact(history)
                    self._record(
                        "context_compacted",
                        {"before_messages": before, "after_messages": len(history)},
                        step,
                        run_id,
                    )
                    self._fire_hook("after_compact", {"messages": len(history)}, step, run_id)
                schemas = self.tool_executor.registry.openai_schemas()
                reply = self._model_reply(history, schemas, step, run_id)
                if self._control is not None:
                    self._control.checkpoint()
                if not isinstance(reply, ModelReply):
                    raise ProviderError("Provider 必须返回 ModelReply")
            except WorkflowStopped as exc:
                error = str(exc)
                self._record("run_unverified", {"error": error}, step, run_id)
                return RunResult(RunStatus.UNVERIFIED, run_id, step, error=error)
            except RequestBudgetExceeded as exc:
                error = str(exc)
                self._record(
                    "request_budget_exhausted",
                    self._stop_context("request_budget_exhausted", error),
                    step,
                    run_id,
                )
                return RunResult(
                    RunStatus.REQUEST_BUDGET_EXHAUSTED,
                    run_id,
                    step,
                    error=error,
                )
            except ExecutionDeadlineExceeded as exc:
                error = str(exc)
                self._record("deadline", self._stop_context("deadline", error), step, run_id)
                return RunResult(RunStatus.DEADLINE, run_id, step, error=error)
            except ExecutionCancelled as exc:
                error = str(exc)
                self._record("cancelled", self._stop_context("user_cancelled", error), step, run_id)
                return RunResult(RunStatus.CANCELLED, run_id, step, error=error)
            except KeyboardInterrupt:
                self._control.cancel()
                error = "用户中断了正在进行的模型请求"
                self._record("cancelled", self._stop_context("user_interrupt", error), step, run_id)
                return RunResult(RunStatus.CANCELLED, run_id, step, error=error)
            except Exception as exc:  # Provider 是可插拔边界，需把未知实现错误转成结果。
                error = f"Provider 错误：{exc}"
                self._record("provider_error", {"error": error}, step, run_id)
                return RunResult(RunStatus.PROVIDER_ERROR, run_id, step, error=error)

            if guard_generation != self._workflow_generation:
                # 不同职责重新读同一文件不是死循环；阶段内仍检测，跨阶段由总预算约束。
                guard = RepetitionGuard(self.repetition_threshold)
                guard_generation = self._workflow_generation
            self._record("model_reply", reply.to_dict(), step, run_id)
            # 自然语言“完成”只是候选，宿主检查真实工具证据后才能给 completed。
            if reply.final_text is not None:
                verification = self._verify_completion(task, reply.final_text, observations)
                if verification is not None and not verification.accepted:
                    payload = verification.to_dict()
                    payload["final_text"] = reply.final_text
                    self._record("completion_rejected", payload, step, run_id)
                    feedback = {
                        "type": "host_completion_rejected",
                        "reason": verification.reason,
                        "changed_files": list(verification.changed_files),
                        "required_action": "继续使用工具获取验证证据，或明确报告未验证状态",
                    }
                    self._append_message(
                        history,
                        {
                            "role": "user",
                            "content": json.dumps(feedback, ensure_ascii=False),
                        },
                    )
                    if step == self.max_steps:
                        error = f"模型声明完成，但宿主验收未通过：{verification.reason}"
                        self._record("run_unverified", {"error": error, **payload}, step, run_id)
                        return RunResult(
                            RunStatus.UNVERIFIED,
                            run_id,
                            step,
                            final_text=reply.final_text,
                            error=error,
                            verification=verification.to_dict(),
                        )
                    continue
                self._append_message(
                    history,
                    reply.to_message(),
                )
                verification_data = verification.to_dict() if verification is not None else None
                if verification_data is not None:
                    self._record("completion_verified", verification_data, step, run_id)
                self._record(
                    "run_completed",
                    {"final_text": reply.final_text, "verification": verification_data},
                    step,
                    run_id,
                )
                return RunResult(
                    RunStatus.COMPLETED,
                    run_id,
                    step,
                    final_text=reply.final_text,
                    verification=verification_data,
                )

            calls = reply.tool_calls
            if not calls:  # ModelReply 自身已校验，保留防御性分支。
                error = "模型回复缺少工具调用"
                self._record("run_failed", {"error": error}, step, run_id)
                return RunResult(RunStatus.FAILED, run_id, step, error=error)
            if len(calls) > self.max_tool_calls_per_reply:
                error = (
                    f"模型单轮返回 {len(calls)} 个工具调用，超过上限 "
                    f"{self.max_tool_calls_per_reply}"
                )
                self._record("run_failed", {"error": error}, step, run_id)
                return RunResult(RunStatus.FAILED, run_id, step, error=error)
            encoded_calls = json.dumps(
                [call.to_dict() for call in calls],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(encoded_calls) > self.max_tool_call_chars:
                error = f"模型工具调用参数超过字符上限 {self.max_tool_call_chars}"
                self._record("run_failed", {"error": error}, step, run_id)
                return RunResult(RunStatus.FAILED, run_id, step, error=error)

            # 同轮多个调用先以一条 assistant 消息写入，再顺序执行。首版不并行，
            # 避免两个写操作争用同一文件，同时兼容 Chat Completions 协议。
            self._append_message(
                history,
                reply.to_message(),
            )
            repeated = False
            for call_index, call in enumerate(calls):
                try:
                    if self._control is not None:
                        self._control.checkpoint()
                except ExecutionDeadlineExceeded as exc:
                    error = str(exc)
                    self._record_interrupted_tools(
                        history,
                        calls[call_index:],
                        error,
                        step,
                        run_id,
                        current_started=False,
                    )
                    self._record("deadline", self._stop_context("deadline", error), step, run_id)
                    return RunResult(RunStatus.DEADLINE, run_id, step, error=error)
                except ExecutionCancelled as exc:
                    error = str(exc)
                    self._record_interrupted_tools(
                        history,
                        calls[call_index:],
                        error,
                        step,
                        run_id,
                        current_started=False,
                    )
                    self._record(
                        "cancelled", self._stop_context("user_cancelled", error), step, run_id
                    )
                    return RunResult(RunStatus.CANCELLED, run_id, step, error=error)
                self._record("tool_call", call.to_dict(), step, run_id)
                decision = self._fire_hook("before_tool", call.to_dict(), step, run_id)
                if repeated:
                    result = ToolResult(
                        call.id, call.name, False, error="重复调用已触发停止，后续调用未执行"
                    )
                elif not decision.allow:
                    self._record(
                        "hook_blocked", {"tool": call.name, "reason": decision.reason}, step, run_id
                    )
                    result = ToolResult(
                        call.id, call.name, False, error=f"Hook 拒绝：{decision.reason}"
                    )
                else:
                    try:
                        result = self.tool_executor.execute(call, control=self._control)
                    except ExecutionDeadlineExceeded as exc:
                        cleanup_failed = bool(getattr(exc, "cleanup_failed", False))
                        error = str(exc) + ("；命令进程组清理失败" if cleanup_failed else "")
                        self._record_interrupted_tools(
                            history,
                            calls[call_index:],
                            error,
                            step,
                            run_id,
                            current_started=True,
                        )
                        self._record(
                            "deadline",
                            self._stop_context(
                                "deadline",
                                error,
                                cleanup_failed=cleanup_failed,
                            ),
                            step,
                            run_id,
                        )
                        return RunResult(RunStatus.DEADLINE, run_id, step, error=error)
                    except ExecutionCancelled as exc:
                        cleanup_failed = bool(getattr(exc, "cleanup_failed", False))
                        error = str(exc) + ("；命令进程组清理失败" if cleanup_failed else "")
                        self._record_interrupted_tools(
                            history,
                            calls[call_index:],
                            error,
                            step,
                            run_id,
                            current_started=True,
                        )
                        self._record(
                            "cancelled",
                            self._stop_context(
                                "user_cancelled",
                                error,
                                cleanup_failed=cleanup_failed,
                            ),
                            step,
                            run_id,
                        )
                        return RunResult(RunStatus.CANCELLED, run_id, step, error=error)
                    except KeyboardInterrupt as exc:
                        self._control.cancel()
                        cleanup_failed = bool(getattr(exc, "cleanup_failed", False))
                        error = "用户中断了正在执行的工具" + (
                            "；命令进程组清理失败" if cleanup_failed else ""
                        )
                        self._record_interrupted_tools(
                            history,
                            calls[call_index:],
                            error,
                            step,
                            run_id,
                            current_started=True,
                        )
                        self._record(
                            "cancelled",
                            self._stop_context(
                                "user_interrupt",
                                error,
                                cleanup_failed=cleanup_failed,
                            ),
                            step,
                            run_id,
                        )
                        return RunResult(RunStatus.CANCELLED, run_id, step, error=error)
                # 拒绝、失败也形成真实观察；模型下一轮才能据此恢复，而不是猜测执行结果。
                observations.append(ToolObservation(call, result))
                self._fire_hook("after_tool", result.to_dict(), step, run_id)
                self._record("tool_result", result.to_dict(), step, run_id)
                self._append_message(
                    history,
                    {
                        "role": "tool",
                        # 用调用 ID 配对，而非工具名：同轮可多次调用同一种工具。
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result.to_dict(), ensure_ascii=False),
                    },
                )
                observe_tool = getattr(self.provider, "observe_tool", None)
                if callable(observe_tool):
                    try:
                        observe_tool(call, result)
                    except Exception:
                        error = "工作流接收工具证据失败，已停止；已执行的工具不会重放"
                        self._record("provider_error", {"error": error}, step, run_id)
                        return RunResult(RunStatus.PROVIDER_ERROR, run_id, step, error=error)
                repeated = repeated or guard.observe(call, result)
            if repeated:
                error = "相同工具参数和结果反复出现，已停止无进展循环"
                self._record(
                    "loop_detected",
                    self._stop_context(
                        guard.stop_reason,
                        error,
                        tool_name=guard.tool_name,
                        policy_reason=guard.policy_reason,
                    ),
                    step,
                    run_id,
                )
                return RunResult(RunStatus.LOOP_DETECTED, run_id, step, error=error)

        error = f"达到最大步骤数 {self.max_steps}，已安全停止"
        self._record("max_steps_reached", {"error": error}, self.max_steps, run_id)
        return RunResult(RunStatus.MAX_STEPS, run_id, self.max_steps, error=error)

    def _stop_context(self, stop_reason: str, error: str, **details: Any) -> dict[str, Any]:
        """构造不含凭据的结构化停止上下文，供 Trace 和评测归因。"""
        remaining = self._control.remaining_seconds() if self._control is not None else None
        workflow = getattr(self.provider, "workflow", None)
        phase = getattr(workflow, "phase", None)
        if hasattr(phase, "value"):
            phase = phase.value
        return {
            "error": error,
            "stop_reason": stop_reason,
            "remaining_seconds": round(remaining, 3) if remaining is not None else None,
            "phase": phase,
            **{key: value for key, value in details.items() if value is not None},
        }

    def _record_interrupted_tools(
        self,
        history: list[dict[str, Any]],
        calls: tuple[ToolCall, ...],
        error: str,
        step: int,
        run_id: str,
        *,
        current_started: bool,
    ) -> None:
        """补齐本轮所有未完成调用，保持协议成对并禁止恢复时自动重放。"""
        for index, call in enumerate(calls):
            started = index == 0 and current_started
            metadata = {"interrupted": True, "replay_allowed": False}
            if not started:
                metadata["not_started"] = True
            message = (
                f"工具执行被中断，副作用状态未知：{error}"
                if started
                else f"同轮前序调用已停止，本工具未执行：{error}"
            )
            result = ToolResult(
                call.id,
                call.name,
                False,
                error=message,
                metadata=metadata,
            )
            self._record("tool_result", result.to_dict(), step, run_id)
            self._append_message(
                history,
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result.to_dict(), ensure_ascii=False),
                },
            )

    def _verify_completion(
        self,
        task: str,
        final_text: str,
        observations: list[ToolObservation],
    ) -> CompletionCheck | None:
        """把完成候选和真实工具证据交给验收器；未配置时返回 None，验收异常按拒绝处理。"""
        if self.completion_verifier is None:
            return None
        try:
            return self.completion_verifier.verify(
                task=task,
                final_text=final_text,
                observations=observations,
            )
        except Exception as exc:  # 自定义验收器失败时必须 fail closed。
            return CompletionCheck(False, f"完成验收器错误：{exc}")

    def _append_message(
        self,
        messages: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        """先更新调用方共享的活动历史，再同步通知持久化回调；存储异常向上传播。"""
        messages.append(message)
        if self.on_message is not None:
            self.on_message(message)

    def _record(self, event: str, payload: Any, step: int, run_id: str) -> None:
        """完整事件脱敏落盘；零碎 delta 只通过实时通道传递。"""
        if self.trace is not None:
            self.trace.record(event, payload, step=step, run_id=run_id)
        self._emit(event, redact(payload), step, run_id)

    def _emit(self, event: str, payload: dict[str, Any], step: int, run_id: str) -> None:
        """事件订阅者只能影响自己的数据副本；显示失败不会重跑工具。"""
        self._event_seq += 1
        if self.on_event is not None:
            try:
                self.on_event(AgentEvent(event, run_id, step, self._event_seq, deepcopy(payload)))
            except Exception:
                if self.trace is not None:
                    self.trace.record("observer_error", {"event": event}, step=step, run_id=run_id)

    def _fire_hook(
        self, name: str, payload: dict[str, Any], step: int, run_id: str
    ) -> HookDecision:
        """Hook 不提供授权能力；拒绝之外仍须经过执行层。"""
        decision, errors = self.hooks.fire(name, payload)
        if self.hooks.has_handlers(name):
            self._record("hook_fired", {"hook": name, "allow": decision.allow}, step, run_id)
        for error in errors:
            self._record("hook_error", {"error": error}, step, run_id)
        return decision

    def _model_reply(
        self, history: list[dict[str, Any]], schemas: list[dict[str, Any]], step: int, run_id: str
    ) -> ModelReply:
        """消费完整 Provider 流后返回消息；终止标志前绝不执行工具。"""
        events_method = getattr(self.provider, "events", None)
        stream_method = getattr(self.provider, "stream", None)
        if callable(events_method):
            events = events_method(history, schemas, stream=self.streaming)
        elif self.streaming and callable(stream_method):
            events = stream_method(history, schemas)
        else:
            reply = self.provider.complete(history, schemas)
            if not isinstance(reply, ModelReply):
                raise ProviderError("Provider 必须返回 ModelReply")
            self.usage.add(reply.usage)
            self._record("usage_updated", self.usage.to_dict(), step, run_id)
            return reply
        reply = None
        try:
            for event in events:
                if reply is not None:
                    raise ProviderError("完整消息后仍收到 Provider 事件")
                # 收到完整消息仍继续消费迭代器，以发现其后的非法事件或传输错误。
                if event.type == "message_complete":
                    reply = event.reply
                    if not isinstance(reply, ModelReply):
                        raise ProviderError("message_complete 缺少 ModelReply")
                elif event.type == "llm_retry":
                    self.usage.retries += 1
                    self._record(event.type, event.data, step, run_id)
                elif event.type == "usage_observed":
                    self.usage.add(TokenUsage.from_dict(event.data.get("usage")))
                    self._record("usage_updated", self.usage.to_dict(), step, run_id)
                elif event.type == "workflow_artifact":
                    # 阶段产物不是用户新授权，固定包裹为带来源的证据数据，不能注入 system 角色。
                    content = json.dumps(
                        {"type": "workflow_artifact", **event.data}, ensure_ascii=False
                    )
                    if len(content) > 24_000:
                        raise ProviderError("工作流交接产物超过持久化上限")
                    self._append_message(history, {"role": "user", "content": content})
                    self._record(event.type, event.data, step, run_id)
                elif event.type in {
                    "model_routed",
                    "route_escalated",
                    "intent_classified",
                    "workflow_stage",
                }:
                    if event.type == "workflow_stage":
                        self._workflow_generation += 1
                    self._record(event.type, event.data, step, run_id)
                else:
                    self._emit(event.type, event.data, step, run_id)
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()
        if reply is None:
            raise ProviderError("模型流缺少完整消息")
        self.usage.add(reply.usage)
        self._record("usage_updated", self.usage.to_dict(), step, run_id)
        return reply


AgentRunner = Agent
