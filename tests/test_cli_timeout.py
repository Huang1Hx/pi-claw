"""CLI 模型请求读超时：优先级、默认值与错误提示。"""

from __future__ import annotations

import io
import urllib.request

import pytest

from interfaces.cli import _request_timeout, main


def _env_file(tmp_path, extra: str = "") -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=fake\n"
        "OPENAI_MODEL=fake-model\n"
        "OPENAI_BASE_URL=https://fixture.invalid/v1\n" + extra,
        encoding="utf-8",
    )


def _run_chat(tmp_path, *extra: str, seen: list[float]) -> int:
    def response(request, **kwargs):
        seen.append(kwargs.get("timeout"))
        return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')

    original = urllib.request.urlopen
    urllib.request.urlopen = response  # type: ignore[assignment]
    try:
        return main(
            [
                "chat",
                "hello",
                "--workspace",
                str(tmp_path),
                "--provider",
                "openai",
                "--no-stream",
                "--print",
                "--data-dir",
                str(tmp_path / "sessions"),
                *extra,
            ]
        )
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]


def test_timeout_precedence_and_default() -> None:
    assert _request_timeout(None, {}) == 120.0
    assert _request_timeout(None, {"OPENAI_TIMEOUT_SECONDS": "77"}) == 77.0
    assert _request_timeout(45, {"OPENAI_TIMEOUT_SECONDS": "77"}) == 45.0


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "abc"])
def test_invalid_env_timeout_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        _request_timeout(None, {"OPENAI_TIMEOUT_SECONDS": raw})


def test_cli_passes_env_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    _env_file(tmp_path, "OPENAI_TIMEOUT_SECONDS=77\n")
    seen: list[float] = []
    assert _run_chat(tmp_path, seen=seen) == 0
    assert seen == [77.0]


def test_cli_timeout_flag_overrides_env(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    _env_file(tmp_path, "OPENAI_TIMEOUT_SECONDS=77\n")
    seen: list[float] = []
    assert _run_chat(tmp_path, "--timeout", "45", seen=seen) == 0
    assert seen == [45.0]


def test_timeout_error_message_reports_configured_seconds(monkeypatch) -> None:
    from providers.openai_compatible import OpenAICompatibleProvider, ProviderError

    def timeout(*_args, **_kwargs):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    provider = OpenAICompatibleProvider(
        api_key="fake", model="fake-model", timeout=120, max_attempts=1
    )
    with pytest.raises(ProviderError) as excinfo:
        list(provider.events([{"role": "user", "content": "hi"}], stream=True))
    assert "读超时（120s" in str(excinfo.value)
    assert "--timeout" in str(excinfo.value)
