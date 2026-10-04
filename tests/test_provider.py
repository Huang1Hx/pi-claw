from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from core.agent import Agent
from core.control import ExecutionCancelled, ExecutionControl
from core.schemas import ModelReply, RunStatus
from providers import OpenAICompatibleProvider, ProviderError, ScriptedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Length": str(len(self.body))}

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def install_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> list[urllib.request.Request]:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        assert timeout == 7
        requests.append(request)
        return FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


def test_openai_provider_parses_text_without_real_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_response(monkeypatch, {"choices": [{"message": {"content": "done"}}]})
    provider = OpenAICompatibleProvider(
        api_key="test-key", base_url="https://example.invalid/v1/", model="test-model", timeout=7
    )
    reply = provider.complete([{"role": "user", "content": "hello"}], [])

    assert reply == ModelReply.final("done")
    assert requests[0].full_url == "https://example.invalid/v1/chat/completions"
    assert requests[0].get_header("Authorization") == "Bearer test-key"
    payload = json.loads(requests[0].data)
    assert payload == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_openai_provider_parses_single_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_response(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "alpha.txt"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    provider = OpenAICompatibleProvider(
        api_key="test-key", base_url="https://example.invalid/v1", model="test-model", timeout=7
    )
    reply = provider.complete([{"role": "user", "content": "read"}], tools)

    assert reply.tool_call is not None
    assert reply.tool_call.name == "read_file"
    assert reply.tool_call.arguments == {"path": "alpha.txt"}
    payload = json.loads(requests[0].data)
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


def test_openai_provider_parses_multiple_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_response(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "one",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "alpha.txt"}',
                                },
                            },
                            {
                                "id": "two",
                                "function": {
                                    "name": "list_files",
                                    "arguments": '{"path": "."}',
                                },
                            },
                        ],
                    }
                }
            ]
        },
    )
    provider = OpenAICompatibleProvider(api_key="test-key", model="test-model", timeout=7)

    reply = provider.complete([{"role": "user", "content": "inspect"}])

    assert [call.id for call in reply.tool_calls] == ["one", "two"]
    assert [call.name for call in reply.tool_calls] == ["read_file", "list_files"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "one",
                                "function": {"name": "read_file", "arguments": "[]"},
                            }
                        ]
                    }
                }
            ]
        },
    ],
)
def test_openai_provider_rejects_malformed_responses(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    install_response(monkeypatch, payload)
    provider = OpenAICompatibleProvider(api_key="test-key", model="test-model", timeout=7)
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "hello"}])


def test_openai_provider_sanitizes_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret-api-key-value"
    detail = f"invalid key {secret}; Authorization: Bearer bearer-secret"

    def fail(_request: object, *, timeout: float) -> None:
        assert timeout == 7
        raise urllib.error.HTTPError(
            "https://example.invalid",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(detail.encode("utf-8")),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    provider = OpenAICompatibleProvider(api_key=secret, model="test-model", timeout=7)
    with pytest.raises(ProviderError) as captured:
        provider.complete([{"role": "user", "content": "hello"}])

    message = str(captured.value)
    assert "HTTP 401" in message
    assert secret not in message
    assert "bearer-secret" not in message
    assert "[REDACTED]" in message


def test_openai_provider_requires_key_and_model_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAICompatibleProvider(model="model").complete([])
    with pytest.raises(ProviderError, match="OPENAI_MODEL"):
        OpenAICompatibleProvider(api_key="key").complete([])


def test_openai_provider_rejects_oversized_response_with_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResponse:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.requested_size: int | None = None

        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            self.requested_size = size
            return b"x" * size

    response = OversizedResponse()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    provider = OpenAICompatibleProvider(
        api_key="private-key",
        model="test-model",
        max_response_bytes=64,
    )

    with pytest.raises(ProviderError, match="超过字节上限 64") as captured:
        provider.complete([{"role": "user", "content": "hello"}])

    assert response.requested_size == 65
    assert "private-key" not in str(captured.value)


def test_scripted_provider_exhaustion_and_reset() -> None:
    provider = ScriptedProvider([ModelReply.final("done")])
    assert provider.remaining == 1
    assert provider.complete([]).final_text == "done"
    with pytest.raises(ProviderError, match="耗尽"):
        provider.complete([])
    provider.reset()
    assert provider.complete([]).final_text == "done"


def test_retry_observes_shared_cancellation_before_second_http_attempt(monkeypatch) -> None:
    """重试事件发出后若任务被取消，不得睡眠完再产生第二次网络请求。"""
    attempts = []

    def fail(*_args, **_kwargs):
        attempts.append(1)
        raise urllib.error.URLError("temporary")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    control = ExecutionControl()
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        max_attempts=3,
        retry_base_delay=10,
        max_retry_delay=10,
        control=control,
    )
    events = provider.events([{"role": "user", "content": "hello"}])

    assert next(events).type == "llm_retry"
    control.cancel()
    with pytest.raises(ExecutionCancelled):
        next(events)
    assert len(attempts) == 1


def test_cancellation_during_response_read_does_not_emit_completed(monkeypatch, workspace) -> None:
    """响应读取期间发生取消时，完整 JSON 也不能覆盖用户取消终态。"""
    control = ExecutionControl()

    class CancellingResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            control.cancel()
            return super().read(size)

    response = CancellingResponse({"choices": [{"message": {"content": "too late"}}]})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    result = Agent(
        OpenAICompatibleProvider(api_key="test-key", model="test-model"),
        ToolExecutor(WorkspacePolicy(workspace)),
    ).run("task", control=control)

    assert result.status is RunStatus.CANCELLED
    assert result.final_text is None


def test_transport_timeout_after_outer_deadline_is_deadline_not_provider_error(
    monkeypatch, workspace
) -> None:
    """阻塞传输跨过外层截止时间后，应按本地 deadline 归因。"""
    now = [10.0]
    control = ExecutionControl(deadline_monotonic=11.0, clock=lambda: now[0])

    def timeout(*_args, **_kwargs):
        now[0] = 11.0
        raise TimeoutError("socket timeout")

    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    result = Agent(
        OpenAICompatibleProvider(
            api_key="test-key", model="test-model", max_attempts=1, timeout=30
        ),
        ToolExecutor(WorkspacePolicy(workspace)),
    ).run("task", control=control)

    assert result.status is RunStatus.DEADLINE
    assert "Provider" not in (result.error or "")
