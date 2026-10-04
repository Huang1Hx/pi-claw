"""在 Agent、工作流与命令后端之间共享截止时间和取消信号。"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class ExecutionStopped(RuntimeError):
    """外层生命周期已停止；调用方不得再发起模型请求或副作用。"""


class ExecutionDeadlineExceeded(ExecutionStopped):
    """整个任务的墙钟截止时间已到。"""


class ExecutionCancelled(ExecutionStopped):
    """用户或上层协调器显式取消了任务。"""


@dataclass(slots=True)
class ExecutionControl:
    """共享单调时钟截止时间与线程安全取消事件。

    ``None`` 表示没有全局截止时间。局部操作仍应保留自己的超时；两者同时
    存在时使用更小值，防止一次模型或命令调用越过整个任务的墙钟预算。
    """

    deadline_monotonic: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        """拒绝无效截止时间和伪造事件，避免边界检查静默失效。"""
        if self.deadline_monotonic is not None and (
            isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or not math.isfinite(float(self.deadline_monotonic))
        ):
            raise ValueError("deadline_monotonic 必须是有限数字或 None")
        if not isinstance(self.cancel_event, threading.Event):
            raise TypeError("cancel_event 必须是 threading.Event")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用")

    def remaining_seconds(self) -> float | None:
        """返回非负剩余秒数；未设置截止时间时返回 ``None``。"""
        if self.deadline_monotonic is None:
            return None
        return max(0.0, float(self.deadline_monotonic) - float(self.clock()))

    def checkpoint(self) -> None:
        """在启动模型请求或副作用前检查取消和截止时间。"""
        if self.cancel_event.is_set():
            raise ExecutionCancelled("任务已被用户或上层协调器取消")
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ExecutionDeadlineExceeded("任务已达到墙钟截止时间")

    def limit_timeout(self, requested: float) -> float:
        """把局部超时收紧到全局余量；没有可用时间时直接停止。"""
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise TypeError("timeout 必须是正数")
        if not math.isfinite(float(requested)) or requested <= 0:
            raise ValueError("timeout 必须是有限正数")
        self.checkpoint()
        remaining = self.remaining_seconds()
        return float(requested) if remaining is None else min(float(requested), remaining)

    def cancel(self) -> None:
        """幂等地广播取消；所有共享该对象的参与者都会观察到。"""
        self.cancel_event.set()
