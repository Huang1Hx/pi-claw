"""配置优先级、密钥隔离、格式校验和 .env 不执行测试。"""

from pathlib import Path

import pytest

from config import load_settings
from config.env import read_env
from providers import OpenAICompatibleProvider

CONFIG = """
[routing]
cheap = "small"
strong = "large"
[models.small]
base_url = "https://small.example/v1"
model = "small-tool-model"
api_key_env = "SMALL_KEY"
input_per_million = 1.0
output_per_million = 2.0
[models.large]
base_url = "https://large.example/v1"
model = "large-tool-model"
api_key_env = "LARGE_KEY"
"""


def test_env_precedence_and_profile_connection_pairs(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('SMALL_KEY="env-secret"\nLARGE_KEY=large-secret\n')
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    monkeypatch.delenv("SMALL_KEY", raising=False)
    settings = load_settings(
        config_path=config, cwd=tmp_path, environ={"SMALL_KEY": "shell-secret"}
    )
    assert settings.profiles["small"].api_key == "shell-secret"
    assert settings.profiles["large"].base_url == "https://large.example/v1"
    assert settings.profiles["large"].api_key == "large-secret"
    assert "secret" not in repr(settings)
    import os

    assert "SMALL_KEY" not in os.environ


def test_env_never_executes_shell_or_interpolates(tmp_path):
    target = tmp_path / "should-not-exist"
    env = tmp_path / ".env"
    env.write_text(f'KEY=$(touch {target})\nOTHER="${{KEY}}"\nEMPTY=\n')
    values = read_env(env)
    assert values["KEY"].startswith("$(touch")
    assert values["OTHER"] == "${KEY}" and values["EMPTY"] == ""
    assert not target.exists()


def test_no_env_and_explicit_path(tmp_path):
    (tmp_path / ".env").write_text("KEY=default\n")
    other = tmp_path / "custom.env"
    other.write_text("KEY=explicit\n")
    assert load_settings(cwd=tmp_path, environ={}, no_env=True).environment == {}
    assert load_settings(env_file=other, environ={}).environment == {"KEY": "explicit"}
    with pytest.raises(ValueError, match="不存在"):
        load_settings(env_file=tmp_path / "missing", environ={})


@pytest.mark.parametrize("value", ["BAD SECRET", 'KEY="unclosed-secret', "KEY=first\nKEY=second"])
def test_invalid_env_does_not_echo_values(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text(value)
    with pytest.raises(ValueError) as error:
        read_env(path)
    assert "SECRET" not in str(error.value) and "secret" not in str(error.value)


@pytest.mark.parametrize(
    "old,new",
    [
        ("https://small.example/v1", "https://user:secret@small.example/v1"),
        ("https://small.example/v1", "https://small.example/v1?key=secret"),
        ("small-tool-model", "<MODEL>"),
        ("input_per_million = 1.0", "input_per_million = -1.0"),
        ("input_per_million = 1.0", "input_per_million = inf"),
        ('cheap = "small"', 'cheap = "missing"'),
        ('cheap = "small"', 'cheap = "small"\nmax_requests = true'),
        ('cheap = "small"', 'cheap = "small"\nmax_requests = 0'),
        ('cheap = "small"', 'cheap = "small"\nunknown = 10'),
    ],
)
def test_invalid_config_rejected(tmp_path, old, new):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.replace(old, new))
    with pytest.raises(ValueError):
        load_settings(config_path=path, environ={}, no_env=True)


def test_explicit_empty_key_never_uses_another_providers_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-secret")
    provider = OpenAICompatibleProvider(
        api_key="", base_url="https://other.example/v1", model="other"
    )
    assert provider.api_key == ""


def test_env_is_ignored_and_template_is_trackable():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "check-ignore", ".env", ".env.example"], cwd=root, capture_output=True, text=True
    )
    assert result.stdout.splitlines() == [".env"]
