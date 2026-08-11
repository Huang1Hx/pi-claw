"""有最大步数、可审计的单 Agent 执行循环。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .providers import Provider, ProviderError
from .schemas import ModelReply, RunResult, RunStatus
from .tools import ApprovalCallback, ToolExecutor
from .trace import TraceWriter


class Agent:
    def __init__(
        self,
        provider: Provider,
        tool_executor: ToolExecutor,
        *,
        approval_callback: ApprovalCallback | None = None,
        trace: TraceWriter | str | Path | None = None,
        max_steps: int = 8,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        self.provider = provider
        self.tool_executor = tool_executor
        if approval_callback is not None:
            self.tool_executor.approval_callback = approval_callback
        self.trace = TraceWriter(trace) if isinstance(trace, (str, Path)) else trace
        self.max_steps = max_steps

    def run(self, task: str) -> RunResult:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task 必须是非空字符串")
        run_id = uuid.uuid4().hex
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        self._record("run_started", {"task": task, "max_steps": self.max_steps}, 0, run_id)

        for step in range(1, self.max_steps + 1):
            try:
                schemas = self.tool_executor.registry.openai_schemas()
                reply = self.provider.complete(messages, schemas)
                if not isinstance(reply, ModelReply):
                    raise ProviderError("Provider 必须返回 ModelReply")
            except Exception as exc:  # Provider 是可插拔边界，需把未知实现错误转成结果。
                error = f"Provider 错误：{exc}"
                self._record("provider_error", {"error": error}, step, run_id)
                return RunResult(RunStatus.PROVIDER_ERROR, run_id, step, error=error)

            self._record("model_reply", reply.to_dict(), step, run_id)
            if reply.final_text is not None:
                self._record("run_completed", {"final_text": reply.final_text}, step, run_id)
                return RunResult(
                    RunStatus.COMPLETED,
                    run_id,
                    step,
                    final_text=reply.final_text,
                )

            call = reply.tool_call
            if call is None:  # ModelReply 自身已校验，保留防御性分支。
                error = "模型回复缺少工具调用"
                self._record("run_failed", {"error": error}, step, run_id)
                return RunResult(RunStatus.FAILED, run_id, step, error=error)

            self._record("tool_call", call.to_dict(), step, run_id)
            result = self.tool_executor.execute(call)
            self._record("tool_result", result.to_dict(), step, run_id)
            # 使用标准 Chat Completions 工具消息格式，把成功、失败和审批拒绝都反馈给模型。
            messages.append(
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
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result.to_dict(), ensure_ascii=False),
                }
            )

        error = f"达到最大步骤数 {self.max_steps}，已安全停止"
        self._record("max_steps_reached", {"error": error}, self.max_steps, run_id)
        return RunResult(RunStatus.MAX_STEPS, run_id, self.max_steps, error=error)

    def _record(
        self, event: str, payload: Any, step: int, run_id: str
    ) -> None:
        if self.trace is not None:
            self.trace.record(event, payload, step=step, run_id=run_id)


AgentRunner = Agent
