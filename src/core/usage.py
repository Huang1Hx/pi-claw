"""服务端实际 Token 用量；缺失 usage 必须与实际零消耗区分。"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次请求的服务端计数，不使用字符估算冒充计费用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> "TokenUsage | None":
        """服务端未返回 usage 时保留未知；畸形计数拒绝解析。"""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("usage 必须是对象")
        prompt, completion = value.get("prompt_tokens"), value.get("completion_tokens")
        if any(type(n) is not int or n < 0 for n in (prompt, completion)):
            raise ValueError("usage Token 计数必须是非负整数")
        total = value.get("total_tokens", prompt + completion)
        if type(total) is not int or total < prompt + completion:
            raise ValueError("usage total_tokens 无效")
        return cls(prompt, completion, total)

    def to_dict(self) -> dict[str, int]:
        """转换为可持久化的计数。"""
        return asdict(self)


class UsageTracker:
    """聚合已完成响应；请求重试和缺失计数单独显示。"""

    def __init__(self) -> None:
        """初始化本回合已知 Token、响应数量与重试计数，缺失 usage 另行记录。"""
        self.requests = 0
        self.requests_with_usage = 0
        self.retries = 0
        self.total = TokenUsage()

    def add(self, usage: TokenUsage | None) -> None:
        """每个完整响应只计一次，包括被宿主拒绝的完成候选。"""
        self.requests += 1
        if usage is not None:
            self.requests_with_usage += 1
            self.total = TokenUsage(
                self.total.prompt_tokens + usage.prompt_tokens,
                self.total.completion_tokens + usage.completion_tokens,
                self.total.total_tokens + usage.total_tokens,
            )

    def to_dict(self) -> dict[str, Any]:
        """仅代表已收到的计数，不保证包含失败请求的服务端计费。"""
        return {
            **self.total.to_dict(),
            "requests": self.requests,
            "requests_with_usage": self.requests_with_usage,
            "requests_without_usage": self.requests - self.requests_with_usage,
            "retries": self.retries,
        }
