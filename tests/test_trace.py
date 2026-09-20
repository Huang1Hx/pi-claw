from __future__ import annotations

import json
from pathlib import Path

import pytest

from persistence.trace import REDACTED, TRUNCATED, TraceReplay, TraceWriter, redact


def test_redact_recurses_and_sanitizes_embedded_text() -> None:
    # 分段构造，既覆盖 sk- 文本脱敏，又避免仓库静态扫描把测试夹具当成真实 Key。
    secret = "sk-" + "abcdefgh12345678"
    value = {
        "api_key": secret,
        "nested": [
            {"github_token": "token-value"},
            ("Bearer abc.def.ghi", "password=hunter2"),
        ],
        "message": f"request failed with {secret}; authorization: Bearer topsecret",
        "safe": "keep-me",
    }
    result = redact(value)
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["api_key"] == REDACTED
    assert result["nested"][0]["github_token"] == REDACTED
    assert result["safe"] == "keep-me"
    assert secret not in encoded
    assert "token-value" not in encoded
    assert "hunter2" not in encoded
    assert "topsecret" not in encoded


def test_trace_writer_persists_only_redacted_payload(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, run_id="fixed-run")
    row = writer.record(
        "provider_error",
        {"error": "api_key=very-secret", "refresh_token": "token-secret"},
        step=3,
    )
    on_disk = path.read_text(encoding="utf-8")

    assert row["run_id"] == "fixed-run"
    assert row["step"] == 3
    assert "very-secret" not in on_disk
    assert "token-secret" not in on_disk
    assert REDACTED in on_disk


def test_redact_covers_vendor_prefixed_environment_variables() -> None:
    value = {
        "OPENAI_API_KEY": "openai-secret",
        "MOONSHOT_API_KEY": "kimi-secret",
        "DASHSCOPE_API_KEY": "qwen-secret",
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "SERVICE_PASSWORD": "password-secret",
        "monkey": "banana",
        "text": (
            "OPENAI_API_KEY=openai-text MOONSHOT_API_KEY: kimi-text DEEPSEEK_TOKEN=deepseek-text"
        ),
    }

    result = redact(value)
    encoded = json.dumps(result, ensure_ascii=False)

    for key in (
        "OPENAI_API_KEY",
        "MOONSHOT_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "SERVICE_PASSWORD",
    ):
        assert result[key] == REDACTED
    assert result["monkey"] == "banana"
    for secret in ("openai-text", "kimi-text", "deepseek-text"):
        assert secret not in encoded


def test_trace_writer_limits_strings_and_event_size(tmp_path: Path) -> None:
    path = tmp_path / "bounded.jsonl"
    writer = TraceWriter(
        path,
        max_string_chars=40,
        max_container_chars=100,
        max_event_chars=300,
    )

    row = writer.record("large", {"content": "x" * 2_000})
    encoded = path.read_text(encoding="utf-8").strip()

    assert len(encoded) <= 300
    assert TRUNCATED in encoded
    assert json.loads(encoded) == row


def test_replay_is_read_only_even_for_tool_call_payload(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.txt"
    trace = tmp_path / "malicious-looking.jsonl"
    TraceWriter(trace).record(
        "tool_call",
        {
            "id": "write-1",
            "name": "write_or_replace",
            "arguments": {"path": str(target), "old": "", "new": "created"},
        },
    )
    before = trace.read_bytes()

    events = TraceReplay(trace).replay()
    formatted = TraceReplay(trace).format_lines(events)

    assert events[0]["event"] == "tool_call"
    assert "write_or_replace" in formatted[0]
    assert trace.read_bytes() == before
    assert not target.exists()


def test_replay_enforces_event_and_byte_budgets(tmp_path: Path) -> None:
    trace = tmp_path / "bounded-replay.jsonl"
    writer = TraceWriter(trace)
    writer.record("first", {"value": 1})
    writer.record("second", {"value": 2})

    with pytest.raises(ValueError, match="事件上限"):
        TraceReplay(trace, max_events=1).read()
    with pytest.raises(ValueError, match="字节上限"):
        TraceReplay(trace, max_bytes=10).read()


@pytest.mark.parametrize("content", ["not-json\n", "[]\n"])
def test_replay_rejects_invalid_rows(tmp_path: Path, content: str) -> None:
    trace = tmp_path / "invalid.jsonl"
    trace.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="轨迹第 1 行"):
        TraceReplay(trace).read()
