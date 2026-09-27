"""共享 API Key 的配置与实际请求字段验证；使用 HTTP 替身，不调用外部服务。"""

import io
import json
import os
import tomllib
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from config import load_settings
from core.agent import Agent
from core.schemas import RunStatus
from interfaces.cli import main
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor

SHARED_CONFIG = """
[connection]
base_url_env = "GATEWAY_URL"
api_key_env = "GATEWAY_KEY"
[models.economy]
model = "small-model"
input_per_million = 1.0
output_per_million = 2.0
[models.primary]
model = "large-model"
input_per_million = 5.0
output_per_million = 10.0
"""
ENVIRONMENT = {"GATEWAY_URL": "https://gateway.invalid/v1", "GATEWAY_KEY": "fake-shared-secret"}


def write_config(tmp_path: Path, content: str = SHARED_CONFIG) -> Path:
    """配置夹具只写临时工作区，不读取项目真实 .env。"""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_shared_connection_env_precedence_and_separate_models(tmp_path, monkeypatch):
    """共享连接解析一次，model 与报价仍按 profile 独立保留。"""
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "GATEWAY_URL=https://old.invalid/v1\nGATEWAY_KEY=old-fake\n", encoding="utf-8"
    )
    settings = load_settings(config_path=write_config(tmp_path), cwd=tmp_path, environ=ENVIRONMENT)
    small, large = settings.profiles["economy"], settings.profiles["primary"]
    assert small.api_key == large.api_key == ENVIRONMENT["GATEWAY_KEY"]
    assert small.base_url == large.base_url == ENVIRONMENT["GATEWAY_URL"]
    assert (small.model, large.model) == ("small-model", "large-model")
    assert (small.input_per_million, large.input_per_million) == (Decimal(1), Decimal(5))
    assert "fake-shared-secret" not in repr(settings)
    assert "fake-shared-secret" not in repr(small)
    assert "GATEWAY_KEY" not in os.environ


def test_shared_direct_url_is_supported(tmp_path):
    """共享地址既可引用环境变量，也可显式配置，结尾斜杠统一规范化。"""
    content = SHARED_CONFIG.replace(
        'base_url_env = "GATEWAY_URL"', 'base_url = "https://direct.invalid/v1/"'
    )
    settings = load_settings(
        config_path=write_config(tmp_path, content), environ=ENVIRONMENT, no_env=True
    )
    assert {p.base_url for p in settings.profiles.values()} == {"https://direct.invalid/v1"}


@pytest.mark.parametrize("own_key", ["fake-other-secret", ""])
def test_complete_profile_override_does_not_inherit_shared_credentials(tmp_path, own_key):
    """换服务必须覆盖整套连接；独立密钥为空也绝不借用共享 Key。"""
    content = SHARED_CONFIG.replace(
        "[models.primary]",
        '[models.primary]\nbase_url = "https://other.invalid/v1"\napi_key_env = "OTHER_KEY"',
    )
    settings = load_settings(
        config_path=write_config(tmp_path, content),
        environ={**ENVIRONMENT, "OTHER_KEY": own_key},
        no_env=True,
    )
    assert settings.profiles["economy"].api_key == ENVIRONMENT["GATEWAY_KEY"]
    assert settings.profiles["primary"].base_url == "https://other.invalid/v1"
    assert settings.profiles["primary"].api_key == own_key


def test_profile_override_supports_url_environment_and_retry_limit(tmp_path):
    """独立供应商可从环境变量读取整套连接，并覆盖全局 HTTP 重试次数。"""
    content = SHARED_CONFIG.replace(
        "[models.primary]",
        '[models.primary]\nbase_url_env = "OTHER_URL"\napi_key_env = "OTHER_KEY"\nmax_attempts = 1',
    )
    settings = load_settings(
        config_path=write_config(tmp_path, content),
        environ={
            **ENVIRONMENT,
            "OTHER_URL": "https://other.invalid/v1/",
            "OTHER_KEY": "fake-other-secret",
        },
        no_env=True,
    )
    profile = settings.profiles["primary"]
    assert profile.base_url == "https://other.invalid/v1"
    assert profile.api_key == "fake-other-secret"
    assert profile.max_attempts == 1
    assert "fake-other-secret" not in repr(profile)


@pytest.mark.parametrize(
    "override",
    [
        'base_url = "https://other.invalid/v1"',
        'base_url_env = "OTHER_URL"',
        'api_key_env = "OTHER_KEY"',
    ],
)
def test_partial_connection_override_is_rejected(tmp_path, override):
    """不能只换目标地址并隐式继承共享密钥，也不能只换密钥而隐式继承地址。"""
    content = SHARED_CONFIG.replace("[models.primary]", f"[models.primary]\n{override}")
    with pytest.raises(ValueError):
        load_settings(config_path=write_config(tmp_path, content), environ=ENVIRONMENT, no_env=True)


@pytest.mark.parametrize(
    "connection",
    [
        "connection = []\n",
        "[connection]\n",
        '[connection]\nbase_url_env = "GATEWAY_URL"\n',
        '[connection]\napi_key_env = "GATEWAY_KEY"\n',
        '[connection]\nbase_url = "https://gateway.invalid/v1"\n'
        'base_url_env = "GATEWAY_URL"\napi_key_env = "GATEWAY_KEY"\n',
        '[connection]\nbase_url_env = "GATEWAY_URL"\napi_key = "fake-hidden-secret"\n',
        '[connection]\nbase_url_env = "GATEWAY_URL"\napi_key_env = "fake-hidden-secret"\n',
        '[connection]\nbase_url_env = "fake-hidden-secret"\napi_key_env = "GATEWAY_KEY"\n',
    ],
)
def test_invalid_connection_shape_is_rejected_without_echoing_values(tmp_path, connection):
    """配置表、地址来源和密钥引用错误都在调用模型前拒绝，且错误不泄露原值。"""
    models = SHARED_CONFIG[SHARED_CONFIG.index("[models.economy]") :]
    with pytest.raises(ValueError) as error:
        load_settings(
            config_path=write_config(tmp_path, connection + models),
            environ=ENVIRONMENT,
            no_env=True,
        )
    assert "fake-hidden-secret" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://gateway.invalid/v1 ",
        "<YOUR_API_URL>",
        "file:///tmp/service",
        "https://user:fake-hidden-secret@gateway.invalid/v1",
        "https://gateway.invalid/v1?key=fake-hidden-secret",
        "https://gateway.invalid/v1#fake-hidden-secret",
        "https://gateway.invalid:fake-hidden-secret/v1",
        "https://[fake-hidden-secret/v1",
        "https://gateway.invalid/\x00fake-hidden-secret",
    ],
)
def test_shared_url_environment_is_validated_without_echoing_values(tmp_path, url):
    """环境变量中的地址与直接配置接受相同校验，不把解析异常中的凭据原文抛出。"""
    with pytest.raises(ValueError) as error:
        load_settings(
            config_path=write_config(tmp_path),
            environ={**ENVIRONMENT, "GATEWAY_URL": url},
            no_env=True,
        )
    assert "fake-hidden-secret" not in str(error.value)


def test_missing_shared_key_does_not_fallback_or_issue_request(tmp_path):
    """共享 Key 缺失时不回退到无关 OPENAI_API_KEY，也不创建联网 Provider。"""
    settings = load_settings(
        config_path=write_config(tmp_path),
        environ={"GATEWAY_URL": ENVIRONMENT["GATEWAY_URL"], "OPENAI_API_KEY": "wrong-fake-key"},
        no_env=True,
    )

    def fail_factory(profile):
        """缺失密钥应在实例化模型客户端之前失败。"""
        pytest.fail("不应创建模型客户端")

    result = Agent(
        RoutedProvider(settings, factory=fail_factory), ToolExecutor(WorkspacePolicy(tmp_path))
    ).run("list files")
    assert result.status is RunStatus.PROVIDER_ERROR
    assert result.routing["requests"] == 0
    assert "wrong-fake-key" not in result.error


def test_route_models_do_not_fallback_to_single_model_environment(tmp_path):
    """每个 profile 必须有自己的 model，不复用单模型入口的 OPENAI_MODEL。"""
    content = SHARED_CONFIG.replace('model = "large-model"', "")
    with pytest.raises(ValueError, match="primary.model"):
        load_settings(
            config_path=write_config(tmp_path, content),
            environ={**ENVIRONMENT, "OPENAI_MODEL": "wrong-single-model"},
            no_env=True,
        )


def test_example_template_loads_after_setting_only_shared_connection_and_model_ids(tmp_path):
    """用户仅填共享地址/Key 和两个 model ID，即可加载根目录模板。"""
    example = Path(__file__).resolve().parents[1] / "pi-claw.example.toml"
    template = example.read_text(encoding="utf-8")
    # 模板可能已填用户选择的模型；仅改测试副本，不覆盖用户模型 ID。
    configured_model = tomllib.loads(template)["models"]["economy"]["model"]
    content = template.replace(configured_model, "small-model").replace(
        "<YOUR_PRIMARY_TOOL_MODEL_ID>", "large-model"
    )
    settings = load_settings(
        config_path=write_config(tmp_path, content),
        environ={
            "OPENAI_BASE_URL": ENVIRONMENT["GATEWAY_URL"],
            "OPENAI_API_KEY": ENVIRONMENT["GATEWAY_KEY"],
        },
        no_env=True,
    )
    assert {p.api_key for p in settings.profiles.values()} == {ENVIRONMENT["GATEWAY_KEY"]}
    assert {p.model for p in settings.profiles.values()} == {"small-model", "large-model"}
    assert settings.routing.intent_strategy == "model"
    assert settings.routing.max_cost is None
    assert all(p.input_per_million is None for p in settings.profiles.values())


@pytest.mark.parametrize("streaming", [False, True])
def test_cli_escalation_reuses_key_and_url_but_changes_request_model(
    tmp_path, monkeypatch, capsys, streaming
):
    """走 CLI→配置→Router→真实 HTTP 序列化；HTTP 收发替身验证同 Key 跨模型升级。"""
    config = write_config(tmp_path)
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    requests = []
    unsafe_call = {
        "id": "unsafe-write",
        "type": "function",
        "function": {
            "name": "write_or_replace",
            "arguments": json.dumps({"path": "must-not-exist.txt", "old": "", "new": "bad"}),
        },
    }

    def response(request, **kwargs):
        """低成本返回越界写候选，主力返回文本；两次用量均须计入费用。"""
        requests.append(request)
        cheap = len(requests) == 1
        message = {"tool_calls": [unsafe_call]} if cheap else {"content": "done"}
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        finish = "tool_calls" if cheap else "stop"
        if streaming:
            delta = {"tool_calls": [{**unsafe_call, "index": 0}]} if cheap else message
            payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            payload["usage"] = usage
            return io.BytesIO(b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n")
        return io.BytesIO(
            json.dumps(
                {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", response)
    trace = tmp_path / "trace.jsonl"
    arguments = [
        "chat",
        "list files",
        "--config",
        str(config),
        "--route",
        "auto",
        "--print",
        "--no-env",
        "--workspace",
        str(tmp_path),
        "--data-dir",
        str(tmp_path / "sessions"),
        "--trace",
        str(trace),
    ]
    if not streaming:
        arguments.append("--no-stream")
    assert main(arguments) == 0
    assert len(requests) == 2
    assert {r.headers["Authorization"] for r in requests} == {"Bearer fake-shared-secret"}
    assert {r.full_url for r in requests} == {"https://gateway.invalid/v1/chat/completions"}
    payloads = [json.loads(r.data) for r in requests]
    assert [p["model"] for p in payloads] == ["small-model", "large-model"]
    assert {t["function"]["name"] for t in payloads[0]["tools"]} == {
        "read_file",
        "list_files",
        "search_text",
    }
    assert len(payloads[1]["tools"]) == 6
    assert [{k: v for k, v in p.items() if k not in {"model", "tools"}} for p in payloads][0] == {
        k: v for k, v in payloads[1].items() if k not in {"model", "tools"}
    }
    assert not (tmp_path / "must-not-exist.txt").exists()
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    summary = next(e["payload"] for e in events if e["event"] == "routing_summary")
    assert summary["known_cost"] == "0.00084"
    assert set(summary["models"]) == {"economy", "primary"}
    assert summary["escalations"] == 1
    output = capsys.readouterr()
    assert "fake-shared-secret" not in output.out + output.err + trace.read_text()
