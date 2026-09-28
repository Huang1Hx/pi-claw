"""Provider 归一化流事件，与 Agent 的 run/step 标识解耦。"""

from dataclasses import dataclass, field
from typing import Any

from core.schemas import ModelReply


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """text_delta、tool_call_delta、llm_retry 和唯一的 message_complete。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    reply: ModelReply | None = None
