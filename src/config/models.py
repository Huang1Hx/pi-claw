"""模型连接和路由预算的数据模型；密钥不进入 repr 或持久元数据。"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ModelProfile:
    """一个已解析模型的端点、模型 ID、密钥和可选报价。

    多个 profile 可继承共享 connection，也可成对覆盖 base_url/api_key 使用独立供应商。
    路由选择 profile 后把 model 作为请求字段发送，不在本机加载模型权重。
    """

    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    thinking_mode: str = "default"
    thinking_format: str = "thinking"
    max_output_tokens: int | None = None
    timeout_seconds: int = 30
    # None 表示沿用 CLI 的全局重试上限；付费模型可单独收紧为 1。
    max_attempts: int | None = None


@dataclass(frozen=True)
class WorkflowSettings:
    """复杂任务的规划/编码/审查角色与有界返工；默认关闭以兼容旧配置。"""

    enabled: bool = False
    planner: str = "planner"
    coder: str = "primary"
    reviewer: str = "reviewer"
    premium_reviewer: str | None = None
    premium_review_policy: str = "off"
    max_planning_requests: int = 4
    max_review_requests: int = 4
    max_premium_review_requests: int = 4
    max_review_rounds: int = 2
    max_context_chars: int = 120_000


@dataclass(frozen=True)
class RoutingSettings:
    """按能力角色分配任务；请求/输出有界，价格不是模型选择依据。"""

    cheap: str = "economy"
    strong: str = "primary"
    max_cheap_requests: int = 2
    max_requests: int = 12
    max_output_tokens: int = 2048
    max_cost: Decimal | None = None
    currency: str = "CNY"
    # 旧配置默认保留本地规则；新模板启用 model，让轻量模型先判断任务复杂度。
    intent_strategy: str = "rules"


@dataclass(frozen=True)
class SandboxSettings:
    """一次启动的命令执行边界；host 是可信工作区兼容模式。"""

    backend: str = "host"
    memory_mib: int = 512
    cpus: float = 1.0
    pids: int = 128
    tmpfs_mib: int = 64
    workspace_tmpfs_mib: int = 256
    network: str = "none"
    image: str = "pi-claw-sandbox:local"


@dataclass(frozen=True)
class Settings:
    """一次启动独立配置；不修改全局 os.environ。"""

    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    config_path: Path | None = None
    env_file: Path | None = None
    workflow: WorkflowSettings = field(default_factory=WorkflowSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
