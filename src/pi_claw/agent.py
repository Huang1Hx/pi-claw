"""有最大步数、可审计的单 Agent 执行循环。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .context import ContextManager
from .providers import Provider, ProviderError
from .schemas import ModelReply, RunResult, RunStatus
from .tools import ApprovalCallback, ToolExecutor
from .trace import TraceWriter

MessageCallback = Callable[[dict[str, Any]], None]


class Agent:
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
    ) -> None:
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
        self.last_messages: list[dict[str, Any]] = []

    def run(
        self,
        task: str,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> RunResult:
        """在给定会话历史上完成一个用户回合。

        v0 调用方不传 ``messages`` 时仍得到一次性运行；交互式 Coding Agent
        传入可变列表，使后续用户输入能继续使用同一上下文。
        """

        if not isinstance(task, str) or not task.strip():
            raise ValueError("task 必须是非空字符串")
        run_id = uuid.uuid4().hex
        history = messages if messages is not None else []
        if not history and self.system_prompt:
            self._append_message(history, {"role": "system", "content": self.system_prompt})
        self._append_message(history, {"role": "user", "content": task})
        self.last_messages = history
        self._record("run_started", {"task": task, "max_steps": self.max_steps}, 0, run_id)

        for step in range(1, self.max_steps + 1):
            try:
                if self.context_manager and self.context_manager.needs_compaction(history):
                    before = len(history)
                    history[:] = self.context_manager.compact(history)
                    self._record(
                        "context_compacted",
                        {"before_messages": before, "after_messages": len(history)},
                        step,
                        run_id,
                    )
                schemas = self.tool_executor.registry.openai_schemas()
                reply = self.provider.complete(history, schemas)
                if not isinstance(reply, ModelReply):
                    raise ProviderError("Provider 必须返回 ModelReply")
            except Exception as exc:  # Provider 是可插拔边界，需把未知实现错误转成结果。
                error = f"Provider 错误：{exc}"
                self._record("provider_error", {"error": error}, step, run_id)
                return RunResult(RunStatus.PROVIDER_ERROR, run_id, step, error=error)

            self._record("model_reply", reply.to_dict(), step, run_id)
            if reply.final_text is not None:
                self._append_message(
                    history,
                    {"role": "assistant", "content": reply.final_text},
                )
                self._record("run_completed", {"final_text": reply.final_text}, step, run_id)
                return RunResult(
                    RunStatus.COMPLETED,
                    run_id,
                    step,
                    final_text=reply.final_text,
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
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ],
                },
            )
            for call in calls:
                self._record("tool_call", call.to_dict(), step, run_id)
                result = self.tool_executor.execute(call)
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

        error = f"达到最大步骤数 {self.max_steps}，已安全停止"
        self._record("max_steps_reached", {"error": error}, self.max_steps, run_id)
        return RunResult(RunStatus.MAX_STEPS, run_id, self.max_steps, error=error)

    def _append_message(
        self,
        messages: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        messages.append(message)
        if self.on_message is not None:
            self.on_message(message)

    def _record(self, event: str, payload: Any, step: int, run_id: str) -> None:
        if self.trace is not None:
            self.trace.record(event, payload, step=step, run_id=run_id)


AgentRunner = Agent
