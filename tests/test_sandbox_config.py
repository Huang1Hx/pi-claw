"""沙箱配置必须显式选择，且错误配置在模型调用前被拒绝。"""

import pytest

from config import load_settings
from interfaces.cli import main
from sandbox.docker import DockerCommandRunner

MODEL = """
[models.primary]
model = "fixture"
base_url = "https://fixture.invalid/v1"
api_key_env = "TEST_KEY"
"""


def test_default_backend_is_host(tmp_path):
    assert load_settings(cwd=tmp_path, environ={}, no_env=True).sandbox.backend == "host"


def test_explicit_docker_limits(tmp_path):
    config = tmp_path / "models.toml"
    config.write_text(MODEL + '[sandbox]\nbackend = "docker"\nmemory_mib = 256\npids = 64\n')
    sandbox = load_settings(config_path=config, environ={"TEST_KEY": "test"}).sandbox
    assert (sandbox.backend, sandbox.memory_mib, sandbox.pids) == ("docker", 256, 64)
    assert sandbox.network == "none"


@pytest.mark.parametrize(
    "body",
    [
        'backend = "unknown"',
        "backend = true",
        "memory_mib = 0",
        "memory_mib = true",
        "cpus = 0",
        "pids = -1",
        'network = "host"',
    ],
)
def test_bad_sandbox_config_rejected(tmp_path, body):
    config = tmp_path / "models.toml"
    config.write_text(MODEL + "[sandbox]\n" + body + "\n")
    with pytest.raises(ValueError, match="sandbox"):
        load_settings(config_path=config, environ={"TEST_KEY": "test"})


def test_env_cannot_select_sandbox(tmp_path):
    (tmp_path / ".env").write_text("PI_CLAW_SANDBOX=docker\n")
    assert load_settings(cwd=tmp_path, environ={}).sandbox.backend == "host"


def test_cli_docker_preflight_fails_before_model_call(tmp_path, monkeypatch, capsys):
    def unavailable(self):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(DockerCommandRunner, "preflight", unavailable)
    result = main(["run", "hello", "--workspace", str(tmp_path), "--sandbox", "docker"])
    assert result == 2
    assert "Docker unavailable" in capsys.readouterr().err
