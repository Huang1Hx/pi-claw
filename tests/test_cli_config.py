"""从 CLI 到配置、路由、HTTP 替身的集成验证，不访问网络。"""

import io
import json
import urllib.request

from interfaces.cli import main


def test_cli_loads_env_from_start_directory_not_workspace(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / ".env").write_text("OPENAI_API_KEY=wrong-target\n")
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=fake-local\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL=https://fixture.invalid/v1\n"
    )
    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    seen = []

    def response(request, **kwargs):
        seen.append(request)
        return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')

    monkeypatch.setattr(urllib.request, "urlopen", response)
    assert (
        main(
            [
                "chat",
                "hello",
                "--workspace",
                str(target),
                "--provider",
                "openai",
                "--no-stream",
                "--print",
                "--data-dir",
                str(tmp_path / "sessions"),
            ]
        )
        == 0
    )
    assert seen[0].headers["Authorization"] == "Bearer fake-local"
    assert seen[0].full_url == "https://fixture.invalid/v1/chat/completions"


def test_cli_routes_with_paired_keys_and_output_limit(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text("""
[models.economy]
model = "small"
base_url = "https://small.invalid/v1"
api_key_env = "SMALL_KEY"
[models.primary]
model = "large"
base_url = "https://large.invalid/v1"
api_key_env = "LARGE_KEY"
""")
    (tmp_path / ".env").write_text("SMALL_KEY=small-fake\nLARGE_KEY=large-fake\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMALL_KEY", raising=False)
    monkeypatch.delenv("LARGE_KEY", raising=False)
    requests = []

    def response(request, **kwargs):
        requests.append(request)
        return io.BytesIO(
            b'{"choices":[{"message":{"content":"done"}}], '
            b'"usage":{"prompt_tokens":2,"completion_tokens":1}}'
        )

    monkeypatch.setattr(urllib.request, "urlopen", response)
    assert (
        main(
            [
                "chat",
                "list files",
                "--config",
                str(config),
                "--route",
                "auto",
                "--no-stream",
                "--print",
                "--data-dir",
                str(tmp_path / "sessions"),
            ]
        )
        == 0
    )
    assert requests[0].headers["Authorization"] == "Bearer small-fake"
    assert requests[0].full_url == "https://small.invalid/v1/chat/completions"
    assert json.loads(requests[0].data)["max_tokens"] == 2048
    output = capsys.readouterr().out
    assert "small-fake" not in output and "route> economy" in output
    assert "--config" in output and "--env-file" in output
