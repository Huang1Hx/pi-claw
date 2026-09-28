"""运行时和界面之间的事件协议，不耦合终端实现。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一次运行内 seq 严格递增；delta 是尚未验收的候选文本。"""

    type: str
    run_id: str
    step: int
    seq: int
    data: dict[str, Any] = field(default_factory=dict)
