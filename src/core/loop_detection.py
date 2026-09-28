"""检测近期重复且没有结果变化的工具调用，限制无进展循环。"""

import hashlib
import json
from collections import deque

from core.schemas import ToolCall, ToolResult


class RepetitionGuard:
    """滑动窗口内相同参数和结果达到阈值时停止；call id 不参与比较。"""

    def __init__(self, threshold: int = 3, window: int = 12) -> None:
        """校验重复阈值并创建有界观察窗口；每个 Agent 回合使用独立实例。"""
        if threshold < 2 or window < threshold:
            raise ValueError("重复阈值至少为 2，窗口不得小于阈值")
        self.threshold = threshold
        self.history: deque[str] = deque(maxlen=window)

    def observe(self, call: ToolCall, result: ToolResult) -> bool:
        """成功写入视为进展并重置窗口；其他调用按真实结果比较。"""
        if call.name in {"write_or_replace", "apply_patch"} and result.ok:
            self.history.clear()
            return False
        payload = [call.name, call.arguments, result.ok, result.output, result.error]
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        self.history.append(fingerprint)
        return self.history.count(fingerprint) >= self.threshold
