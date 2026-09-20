"""显式注入的生命周期 Hooks；不从仓库自动加载或执行脚本。"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HookDecision:
    """阻止一次工具调用；允许仅表示继续现有 Policy/Approval 流程。"""

    allow: bool = True
    reason: str = ""


class HookBus:
    """受信宿主注册 Python 回调；数据副本防止回调更改待审批操作。"""

    EVENTS = frozenset(
        {"run_start", "run_end", "before_tool", "after_tool", "before_compact", "after_compact"}
    )

    def __init__(self) -> None:
        """初始化本实例的回调表；只有宿主显式注册的回调才会执行。"""
        self._handlers: dict[str, list[Callable]] = {}

    def register(self, event: str, callback: Callable) -> None:
        """注册 callback(data) -> HookDecision | None，同一事件按注册顺序同步执行。

        data 是事件数据副本；仅 before_tool 的拒绝有阻断作用，None 表示继续。
        回调是受信任宿主 Python 代码，不能把数据深拷贝理解为代码执行沙箱。
        """
        if event not in self.EVENTS or not callable(callback):
            raise ValueError("未知 Hook 事件或无效回调")
        self._handlers.setdefault(event, []).append(callback)

    def fire(self, event: str, data: dict[str, Any]) -> tuple[HookDecision, list[str]]:
        """before_tool 异常默认拒绝；观察类 Hook 异常只返回诊断。"""
        errors: list[str] = []
        for handler in self._handlers.get(event, []):
            try:
                # 每个回调得到独立副本，不能通过修改参数改变待审批的工具调用。
                decision = handler(deepcopy(data))
                if decision is not None and not isinstance(decision, HookDecision):
                    raise TypeError("Hook 必须返回 HookDecision 或 None")
                if event == "before_tool" and decision is not None and not decision.allow:
                    return decision, errors
            except Exception as exc:
                errors.append(f"{event}: {type(exc).__name__}")
                if event == "before_tool":
                    return HookDecision(False, "前置 Hook 失败，已阻止工具执行"), errors
        return HookDecision(), errors

    def has_handlers(self, event: str) -> bool:
        """供运行时记录真正触发的扩展事件。"""
        return bool(self._handlers.get(event))
