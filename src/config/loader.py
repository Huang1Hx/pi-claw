"""环境与显式 TOML 的加载、校验；不从目标仓库向上搜索配置。"""

import os
import re
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

from config.connections import resolve_connection
from config.env import read_env
from config.models import ModelProfile, RoutingSettings, Settings, WorkflowSettings


def _price(value: object, label: str) -> Decimal | None:
    """把可选单价转换为有限、非负 Decimal；缺省返回 None，表示报价未知。"""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label} 必须是非负数") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} 必须是有限非负数")
    return number


def _string(value: object, label: str) -> str:
    """校验配置文本，拒绝空白、控制符和未替换占位符；错误不回显原值。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} 必须是无首尾空白的非空字符串")
    if any(c in value for c in "\r\n\t<>"):
        raise ValueError(f"{label} 含控制符或未替换占位符")
    return value


def load_settings(
    *,
    config_path: Path | None = None,
    env_file: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    no_env: bool = False,
) -> Settings:
    """加载本次启动的独立 Settings，不修改进程环境或自动搜索目标仓库。

    cwd 仅决定默认 .env 的位置；显式 env_file 必须存在，no_env 只禁用文件读取。
    environ 可注入测试环境；未提供则读取 os.environ，且其值覆盖 .env 同名变量。
    config_path 显式指定模型 profile 的 TOML；可通过 connection 共用地址和密钥，
    每个 profile 只写不同的 model。密钥始终通过 api_key_env 引用环境值。
    无效结构和预算在请求模型之前抛出 ValueError；错误不得回显密钥原文。
    """
    environment = (
        {}
        if no_env
        else read_env(env_file or (cwd or Path.cwd()) / ".env", required=env_file is not None)
    )
    environment.update(os.environ if environ is None else environ)
    source_env = None if no_env else (env_file or (cwd or Path.cwd()) / ".env").resolve()
    if config_path is None:
        return Settings(environment=environment, env_file=source_env)
    with config_path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("TOML 配置超过 64 KiB")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ValueError("TOML 配置格式无效") from None
    if set(data) - {"connection", "models", "routing", "workflow"}:
        raise ValueError("未知 TOML 顶层配置项")
    shared_connection = (
        resolve_connection(data["connection"], environment, label="connection")
        if "connection" in data
        else None
    )
    models = data.get("models", {})
    if not isinstance(models, dict) or not models or len(models) > 16:
        raise ValueError("models 必须包含 1–16 个 profile")
    profiles = {}
    for name, model in models.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,31}", name) or name in {"auto", "scripted"}:
            raise ValueError("profile 名称无效或占用保留名称")
        allowed = {
            "base_url",
            "model",
            "api_key_env",
            "input_per_million",
            "output_per_million",
            "thinking_mode",
            "thinking_format",
            "max_output_tokens",
            "timeout_seconds",
        }
        if not isinstance(model, dict) or set(model) - allowed:
            raise ValueError(f"profile {name} 包含未知配置项")
        overrides = {key: model[key] for key in ("base_url", "api_key_env") if key in model}
        # 两个字段都省略才继承共享连接；部分覆盖直接校验失败，不能把共享 Key 发往新地址。
        url, api_key = (
            resolve_connection(overrides, environment, label=f"models.{name}")
            if overrides or shared_connection is None
            else shared_connection
        )
        thinking_mode = model.get("thinking_mode", "default")
        thinking_format = model.get("thinking_format", "thinking")
        if not isinstance(thinking_mode, str) or thinking_mode not in {
            "default",
            "enabled",
            "disabled",
        }:
            raise ValueError(f"{name}.thinking_mode 必须是 default、enabled 或 disabled")
        if not isinstance(thinking_format, str) or thinking_format not in {
            "thinking",
            "chat_template_kwargs",
            "enable_thinking",
        }:
            raise ValueError(f"{name}.thinking_format 不受支持")
        max_output = model.get("max_output_tokens")
        timeout = model.get("timeout_seconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError(f"{name}.timeout_seconds 必须是 1–300 的整数")
        if max_output is not None and (type(max_output) is not int or not 1 <= max_output <= 32768):
            raise ValueError(f"{name}.max_output_tokens 必须是 1–32768 的整数")
        profiles[name] = ModelProfile(
            name,
            url,
            _string(model.get("model"), f"{name}.model"),
            api_key,
            _price(model.get("input_per_million"), "input_per_million"),
            _price(model.get("output_per_million"), "output_per_million"),
            thinking_mode,
            thinking_format,
            max_output,
            timeout,
        )
    routing_data = data.get("routing", {})
    allowed = set(RoutingSettings.__dataclass_fields__)
    if not isinstance(routing_data, dict) or set(routing_data) - allowed:
        raise ValueError("routing 包含未知配置项")
    values = dict(routing_data)
    values["max_cost"] = _price(values.get("max_cost"), "max_cost")
    routing = RoutingSettings(**values)
    if not isinstance(routing.intent_strategy, str) or routing.intent_strategy not in {
        "rules",
        "model",
    }:
        raise ValueError("routing.intent_strategy 必须是 rules 或 model")
    for key, upper in (
        ("max_requests", 100),
        ("max_cheap_requests", 20),
        ("max_output_tokens", 32768),
    ):
        value = getattr(routing, key)
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(f"routing.{key} 必须是 1–{upper} 的整数")
    _string(routing.currency, "currency")
    for key in ("cheap", "strong"):
        name = getattr(routing, key)
        if key in routing_data and (not isinstance(name, str) or name not in profiles):
            raise ValueError("routing.cheap/strong 必须引用已定义的 profile")
    workflow_data = data.get("workflow", {})
    if not isinstance(workflow_data, dict) or set(workflow_data) - set(
        WorkflowSettings.__dataclass_fields__
    ):
        raise ValueError("workflow 包含未知配置项")
    workflow = WorkflowSettings(**workflow_data)
    if type(workflow.enabled) is not bool:
        raise ValueError("workflow.enabled 必须是布尔值")
    for key, lower, upper in (
        ("max_planning_requests", 1, 12),
        ("max_review_requests", 1, 12),
        ("max_review_rounds", 1, 5),
        ("max_context_chars", 4000, 500_000),
    ):
        value = getattr(workflow, key)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"workflow.{key} 必须是 {lower}–{upper} 的整数")
    for key in ("planner", "coder", "reviewer"):
        name = _string(getattr(workflow, key), f"workflow.{key}")
        if workflow.enabled and name not in profiles:
            raise ValueError(f"workflow.{key} 必须引用已定义的 profile")
    return Settings(profiles, routing, environment, config_path.resolve(), source_env, workflow)
