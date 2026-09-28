"""解析可复用的 API 连接；多个模型可共用地址和密钥，但跨地址覆盖必须显式配对。"""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit


def _env_name(value: object, label: str) -> str:
    """仅接受环境变量名，错误不回显输入，防止把误填的真实密钥写进日志。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        raise ValueError(f"{label} 必须是环境变量名")
    return value


def resolve_connection(
    value: object,
    environment: Mapping[str, str],
    *,
    label: str,
) -> tuple[str, str]:
    """返回 (API 根地址, 密钥)，供多个 ModelProfile 显式复用。

    base_url 与 base_url_env 必须二选一，api_key_env 必填；密钥只能引用环境变量。
    环境缺失密钥时保留空值，由 Router 在联网前拒绝，不回退到其他供应商的 Key。
    本函数不合并半份连接配置，避免更换地址时无意继承共享密钥。
    """
    allowed = {"base_url", "base_url_env", "api_key_env"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{label} 必须是只包含 API 地址和密钥变量名的配置表")
    if ("base_url" in value) == ("base_url_env" in value):
        raise ValueError(f"{label} 必须且只能指定 base_url 或 base_url_env")
    key_env = _env_name(value.get("api_key_env"), f"{label}.api_key_env")
    if "base_url_env" in value:
        url_env = _env_name(value["base_url_env"], f"{label}.base_url_env")
        url = environment.get(url_env, "")
    else:
        url = value["base_url"]
    if (
        not isinstance(url, str)
        or not url
        or any(char.isspace() or char in "<>" or ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError(f"{label} 缺少有效 API 地址，请检查 base_url 或对应环境变量")
    # URL 解析异常可能包含原始输入；配置错误一律改为不带输入内容的诊断。
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
        )
        # 访问 port 属性会校验非数字/越界端口，异常统一在下方脱敏。
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{label}.base_url 必须是无凭据和 query 的 HTTP(S) 地址")
    return url.rstrip("/"), environment.get(key_env, "")
