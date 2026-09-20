"""确定性离线模型，验证运行时而非真实模型能力。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from core.schemas import ModelReply, ToolCall
from providers.base import ProviderError


class ScriptedProvider:
    """按顺序返回预设回复，完全离线且可重复。"""

    def __init__(self, replies: Iterable[ModelReply]) -> None:
        """固化并校验预设回复序列、初始化游标；不连接外部模型服务。"""
        self._replies = tuple(replies)
        self._index = 0
        if not self._replies:
            raise ValueError("ScriptedProvider 至少需要一条回复")
        if not all(isinstance(reply, ModelReply) for reply in self._replies):
            raise TypeError("replies 中每项都必须是 ModelReply")

    @property
    def remaining(self) -> int:
        """返回尚未消费的脚本回复数量。"""
        return len(self._replies) - self._index

    def reset(self) -> None:
        """重置离线脚本游标以便重复实验。"""
        self._index = 0

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """忽略 messages/tools，按游标返回下一条预设回复；脚本耗尽抛出 ProviderError。"""
        del messages, tools
        if self._index >= len(self._replies):
            raise ProviderError("脚本回复已耗尽，Agent 尚未收到 final_text")
        reply = self._replies[self._index]
        self._index += 1
        return reply

    def reply(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """兼容 complete 的调用别名。"""
        return self.complete(messages, tools)


class OfflineCodingProvider:
    """可连续多轮使用的确定性离线 Provider。

    它只做工作区文件枚举，用于验证 REPL、Session 和 Context；不会把固定脚本
    冒充真实模型，也不会修改用户仓库。
    """

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """消费上下文并返回一次完整的模型回复。"""
        del tools
        if not messages:
            raise ProviderError("离线 Provider 没有收到消息")
        latest = messages[-1]
        if latest.get("role") == "tool":
            try:
                result = json.loads(str(latest.get("content", "{}")))
                output = result.get("output", {})
                files = output.get("files", []) if isinstance(output, dict) else []
                count = len(files) if isinstance(files, list) else 0
                truncated = bool(output.get("truncated")) if isinstance(output, dict) else False
                reason = output.get("reason") if isinstance(output, dict) else None
            except (json.JSONDecodeError, AttributeError, TypeError):
                count = 0
                truncated = False
                reason = None
            boundary = f"；结果已截断（{reason}）" if truncated else ""
            return ModelReply.final(
                f"离线 Coding Agent 已完成只读检查，共看到 {count} 个文件{boundary}。"
                "该模式用于验证会话与工具链，不声称理解或修改了代码。"
            )
        return ModelReply.call(
            ToolCall(
                id=f"offline-{uuid.uuid4().hex[:12]}",
                name="list_files",
                arguments={"path": "."},
            )
        )
