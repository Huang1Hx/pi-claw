"""根据服务端 usage 和本地配置价格估算费用；不冒充供应商账单。"""

from decimal import Decimal

from config.models import ModelProfile
from core.usage import TokenUsage


class CostLedger:
    """按 profile 汇总完整响应；失败或缺失 usage 会明确降低费用可信度。"""

    def __init__(self, currency: str, *, enabled: bool = True) -> None:
        """建立按模型 profile 分组的费用账本，分别记录已知费用和费用未知的请求。"""
        self.currency = currency
        self.enabled = enabled
        self.known_cost = Decimal(0)
        self.unknown_requests = 0
        self.models: dict[str, dict] = {}

    def add(self, profile: ModelProfile, usage: TokenUsage | None) -> None:
        """不将缓存折扣或失败重试的费用猜作零。"""
        initial = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        if self.enabled:
            initial["known_cost"] = "0"
        row = self.models.setdefault(profile.name, initial)
        row["requests"] += 1
        if usage is not None:
            row["prompt_tokens"] += usage.prompt_tokens
            row["completion_tokens"] += usage.completion_tokens
        if not self.enabled:
            # 未配置价格时仅统计请求和 Token，不输出猜测金额或“费用未知”的告警。
            return
        if usage is None or profile.input_per_million is None or profile.output_per_million is None:
            self.unknown_requests += 1
            return
        # 输入/输出各按本地配置的“每百万 Token 单价”计算，未知用量不虚构为零费用。
        cost = (
            usage.prompt_tokens * profile.input_per_million
            + usage.completion_tokens * profile.output_per_million
        ) / 1_000_000
        self.known_cost += cost
        row["known_cost"] = str(Decimal(row["known_cost"]) + cost)

    def to_dict(self) -> dict:
        """数字金额以字符串序列化，避免二进制浮点误差。"""
        result = {"models": {k: dict(v) for k, v in self.models.items()}}
        if self.enabled:
            result.update(
                currency=self.currency,
                known_cost=str(self.known_cost),
                unknown_requests=self.unknown_requests,
            )
        return result
