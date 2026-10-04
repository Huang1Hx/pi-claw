"""模型 Provider 协议与标准异常。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from core.schemas import ModelReply


class ProviderError(RuntimeError):
    """Provider 的可读、已脱敏错误。"""


class RequestBudgetExceeded(ProviderError):
    """宿主配置的模型请求次数已耗尽，不是网络或供应商故障。"""


class WorkflowStopped(ProviderError):
    """工作流证据、权限或审查上限不满足；不能当作完成或网络故障。"""


@runtime_checkable
class Provider(Protocol):
    """可替换模型接口；离线与 HTTP 实现共用消息结构。

    实现 complete 即可接入 Agent，不必继承具体 Provider 类。流式 events/stream
    是可选扩展，由 Agent 按能力检测；Provider 负责模型通信，不负责工具授权或执行。
    """

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """发送完整上下文并返回一条已校验的模型消息。

        messages 是按协议排序的 system/user/assistant/tool 消息，实现应视其为只读。
        tools 是模型可见的参数 Schema，不是可直接执行的 Python 函数。
        返回值只允许最终文本候选或一组 ToolCall；失败应抛出已脱敏的 ProviderError。
        ModelReply 合法不等于任务完成，工具审批与最终验收仍由宿主处理。
        """
        ...
