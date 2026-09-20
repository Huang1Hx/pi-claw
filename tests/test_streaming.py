"""离线 HTTP/SSE 合约测试，不访问真实模型、不读取真实 API key。"""

import io
import json
import urllib.error
import urllib.request

import pytest

from core.agent import Agent
from core.schemas import RunStatus
from providers import OpenAICompatibleProvider, ProviderError
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def frame(delta=None, finish=None, usage=None):
    """生成模拟服务端帧，支持 usage-only 消息。"""
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value = {"choices": [], "usage": usage}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def provider(**kwargs):
    """所有请求均被测试替身截获。"""
    return OpenAICompatibleProvider(api_key="fake-key", model="fake-model", **kwargs)


def test_text_deltas_and_actual_usage(monkeypatch):
    raw = frame({"content": "你好"}) + frame({"content": "世界"}, "stop")
    raw += frame(usage={"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24})
    raw += b"data: [DONE]\n\n"
    requests = []

    def open_fake(req, **kwargs):
        requests.append(json.loads(req.data))
        return io.BytesIO(raw)

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    events = list(provider().stream([]))
    assert [e.type for e in events] == ["text_delta", "text_delta", "message_complete"]
    assert events[-1].reply.final_text == "你好世界"
    assert events[-1].reply.usage.total_tokens == 24
    assert requests[0]["stream_options"] == {"include_usage": True}


def test_interleaved_tool_json_is_assembled_before_execution(monkeypatch, workspace):
    def fragment(i, **kw):
        return {"tool_calls": [{"index": i, **kw}]}

    raw = frame(fragment(0, id="read", function={"name": "read_file", "arguments": '{"pa'}))
    raw += frame(fragment(1, id="list", function={"name": "list_files", "arguments": "{}"}))
    raw += frame(fragment(0, function={"arguments": 'th":"alpha.txt"}'}), "tool_calls")
    raw += b"data: [DONE]\n\n"
    responses = [raw, frame({"content": "done"}, "stop") + b"data: [DONE]\n\n"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(responses.pop(0)))
    events = []
    agent = Agent(
        provider(), ToolExecutor(WorkspacePolicy(workspace)), streaming=True, on_event=events.append
    )
    result = agent.run("inspect")
    assert result.ok
    assert [m["tool_call_id"] for m in agent.last_messages if m["role"] == "tool"] == [
        "read",
        "list",
    ]
    assert events[-1].type == "run_end"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


@pytest.mark.parametrize(
    "raw",
    [
        frame({"content": "partial"}),
        frame({"content": "partial"}, "length") + b"data: [DONE]\n\n",
        frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "read",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ]
            },
            "stop",
        )
        + b"data: [DONE]\n\n",
        b"data: not-json\n\ndata: [DONE]\n\n",
        b"data: [DONE]\n\n",
    ],
)
def test_incomplete_or_malformed_stream_rejected(monkeypatch, raw):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(raw))
    with pytest.raises(ProviderError):
        list(provider().stream([]))


def test_stream_read_budget(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"x" * 200))
    with pytest.raises(ProviderError, match="字节上限"):
        list(provider(max_response_bytes=64).stream([]))


def test_retry_429_then_success_and_usage(monkeypatch, workspace):
    attempts, waits = [], []

    def open_fake(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                "https://invalid", 429, "busy", {"Retry-After": "99"}, io.BytesIO(b"busy")
            )
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 2},
                }
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    events = []
    result = Agent(
        provider(sleep=waits.append),
        ToolExecutor(WorkspacePolicy(workspace)),
        on_event=events.append,
    ).run("task")
    assert result.ok and result.usage["total_tokens"] == 11
    assert result.usage["retries"] == 1 and result.usage["requests"] == 1
    assert waits == [10.0]
    assert sum(e.type == "llm_retry" for e in events) == 1


@pytest.mark.parametrize("code,expected", [(400, 1), (401, 1), (429, 3), (503, 3)])
def test_retry_is_bounded_and_auth_errors_are_terminal(monkeypatch, code, expected):
    attempts = []

    def fail(*a, **k):
        attempts.append(1)
        raise urllib.error.HTTPError("https://invalid", code, "error", {}, io.BytesIO(b"fake-key"))

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(ProviderError) as error:
        provider(sleep=lambda _: None).complete([])
    assert len(attempts) == expected
    assert "fake-key" not in str(error.value)


def test_no_replay_after_partial_stream(monkeypatch, workspace):
    attempts = []

    class Interrupted(io.BytesIO):
        def readline(self, size=-1):
            value = super().readline(size)
            if not value:
                raise TimeoutError("disconnected")
            return value

    def open_fake(*a, **k):
        attempts.append(1)
        return Interrupted(
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "write",
                            "function": {
                                "name": "write_or_replace",
                                "arguments": '{"path":"new.txt"',
                            },
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    result = Agent(
        provider(sleep=lambda _: None),
        ToolExecutor(WorkspacePolicy(workspace), approval_callback=lambda *a: True),
        streaming=True,
    ).run("write")
    assert result.status is RunStatus.PROVIDER_ERROR
    assert len(attempts) == 1
    assert not (workspace / "new.txt").exists()


def test_network_retry_before_response(monkeypatch):
    attempts = []

    def open_fake(*a, **k):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("connection reset")
        return io.BytesIO(frame({"content": "done"}, "stop") + b"data: [DONE]\n\n")

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    assert list(provider(sleep=lambda _: None).stream([]))[-1].reply.final_text == "done"
    assert len(attempts) == 3
