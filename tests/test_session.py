from __future__ import annotations

import io
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pi_claw.providers import OfflineCodingProvider
from pi_claw.repl import CodingRepl
from pi_claw.schemas import RunResult, RunStatus
from pi_claw.session import SessionStore
from pi_claw.trace import TraceWriter


def test_session_store_uses_pi_claw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "pi-claw-home"
    monkeypatch.setenv("PI_CLAW_HOME", str(configured))
    assert SessionStore().data_dir == (configured / "sessions").resolve()


def test_session_create_load_append_and_list(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.create(workspace, "system one")
    store.append_message(
        first.session_id,
        workspace,
        {"role": "user", "content": "hello"},
    )
    second = store.create(workspace, "system two")

    loaded = store.load(first.session_id, workspace)
    messages = store.load_messages(first.session_id, workspace)
    listed = store.list_sessions(workspace)

    assert loaded.session_id == first.session_id
    assert messages == [
        {"role": "system", "content": "system one"},
        {"role": "user", "content": "hello"},
    ]
    assert {item.session_id for item in listed} == {first.session_id, second.session_id}
    assert store.latest(workspace).session_id == second.session_id  # type: ignore[union-attr]


def test_session_persists_runtime_without_api_key(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(
        workspace,
        "system",
        provider="openai",
        model="provider-model",
        base_url="https://example.invalid/v1",
    )

    loaded = store.load(session.session_id, workspace)
    assert loaded.provider == "openai"
    assert loaded.model == "provider-model"
    assert loaded.base_url == "https://example.invalid/v1"
    assert "api_key" not in loaded.to_dict()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@example.invalid/v1",
        "https://example.invalid/v1?api_key=secret",
        "https://example.invalid/v1#token",
        "file:///tmp/model",
    ],
)
def test_session_rejects_sensitive_or_non_http_base_url(
    workspace: Path,
    tmp_path: Path,
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="base_url"):
        SessionStore(tmp_path / "sessions").create(
            workspace,
            "system",
            provider="openai",
            base_url=base_url,
        )


def test_session_storage_permissions_are_private(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    group = next(store.data_dir.iterdir())
    session_dir = group / session.session_id

    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(group.stat().st_mode) == 0o700
    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((session_dir / "state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((session_dir / "context.jsonl").stat().st_mode) == 0o600


def test_session_is_scoped_to_workspace(tmp_path: Path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    session = store.create(first_workspace, "system")

    with pytest.raises(ValueError, match="不存在|不属于"):
        store.load(session.session_id, second_workspace)
    assert store.list_sessions(second_workspace) == []


@pytest.mark.parametrize("session_id", ["../escape", "/absolute", "bad id", ""])
def test_session_rejects_unsafe_id(workspace: Path, tmp_path: Path, session_id: str) -> None:
    with pytest.raises(ValueError, match="session_id"):
        SessionStore(tmp_path / "sessions").load(session_id, workspace)


def test_session_state_updates_title_and_deduplicates_approvals(
    workspace: Path, tmp_path: Path
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    updated = store.update_state(
        session.session_id,
        workspace,
        title="  focused task  ",
        approved_tools=["scope-a", "scope-a", "scope-b"],
    )
    assert updated.title == "focused task"
    assert updated.approved_tools == ["scope-a", "scope-b"]


def test_active_messages_round_trip_without_rewriting_raw_history(
    workspace: Path, tmp_path: Path
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    store.append_message(session.session_id, workspace, {"role": "user", "content": "raw history"})
    raw = store.load_messages(session.session_id, workspace)
    active = [raw[0], {"role": "assistant", "content": "compacted summary"}]

    store.save_active_messages(session.session_id, workspace, active)

    assert store.load_active_messages(session.session_id, workspace) == active
    assert store.load_messages(session.session_id, workspace) == raw


def test_session_read_limits_cover_events_and_bytes(workspace: Path, tmp_path: Path) -> None:
    event_path = tmp_path / "event-sessions"
    seed_store = SessionStore(event_path)
    event_session = seed_store.create(workspace, "system")
    seed_store.append_message(
        event_session.session_id, workspace, {"role": "user", "content": "second"}
    )
    event_store = SessionStore(event_path, max_events=1)
    with pytest.raises(ValueError, match="消息上限"):
        event_store.load_messages(event_session.session_id, workspace)

    byte_path = tmp_path / "byte-sessions"
    byte_session = SessionStore(byte_path).create(workspace, "system message larger than limit")
    byte_store = SessionStore(byte_path, max_bytes=20)
    with pytest.raises(ValueError, match="字节上限"):
        byte_store.load_messages(byte_session.session_id, workspace)


def test_active_message_write_limits(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", max_events=1, max_bytes=80)
    session = store.create(workspace, "system")
    with pytest.raises(ValueError, match="消息上限"):
        store.save_active_messages(
            session.session_id,
            workspace,
            [{"role": "system"}, {"role": "user"}],
        )
    with pytest.raises(ValueError, match="写入字节上限"):
        store.save_active_messages(
            session.session_id,
            workspace,
            [{"role": "user", "content": "x" * 200}],
        )


def test_repl_loads_explicit_session_and_latest(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.create(workspace, "system one")
    store.append_message(
        first.session_id,
        workspace,
        {"role": "user", "content": "persisted"},
    )
    latest = store.create(workspace, "system latest")

    explicit = CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        store=store,
        trace=TraceWriter(tmp_path / "explicit.jsonl"),
        session_id=first.session_id,
        output=io.StringIO(),
        interactive_approvals=False,
    )
    continued = CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        store=store,
        trace=TraceWriter(tmp_path / "latest.jsonl"),
        continue_latest=True,
        output=io.StringIO(),
        interactive_approvals=False,
    )

    assert explicit.session.session_id == first.session_id
    assert explicit.messages[-1]["content"] == "persisted"
    assert continued.session.session_id == latest.session_id


def test_journal_rotates_and_active_resume_skips_old_segments(
    workspace: Path,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions", max_segment_bytes=220)
    session = store.create(workspace, "system")
    expected = [{"role": "system", "content": "system"}]
    for index in range(6):
        message = {"role": "user", "content": f"message-{index}-" + "x" * 80}
        store.append_message(session.session_id, workspace, message)
        expected.append(message)

    session_dir = next((store.data_dir).glob(f"*/{session.session_id}"))
    segments = sorted(session_dir.glob("context*.jsonl"))
    assert len(segments) >= 3
    assert store.load_messages(session.session_id, workspace) == expected
    assert store.load_active_messages(session.session_id, workspace) == expected
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in segments)


def test_legacy_active_context_migration_keeps_durable_journal_tail(
    workspace: Path,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    store.append_message(
        session.session_id,
        workspace,
        {"role": "user", "content": "durable tail"},
    )
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    (session_dir / "active_context.json").write_text(
        json.dumps([{"role": "system", "content": "system"}]),
        encoding="utf-8",
    )

    recovered = store.load_active_messages(session.session_id, workspace)

    assert recovered == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "durable tail"},
    ]


def test_stale_session_writer_is_rejected_without_corrupting_journal(
    workspace: Path,
    tmp_path: Path,
) -> None:
    first = SessionStore(tmp_path / "sessions")
    second = SessionStore(tmp_path / "sessions")
    session = first.create(workspace, "system")
    first_messages, first_seq = first.load_active_snapshot(session.session_id, workspace)
    second_messages, second_seq = second.load_active_snapshot(session.session_id, workspace)
    first_message = {"role": "user", "content": "first writer"}
    first_messages.append(first_message)
    first.append_message(
        session.session_id,
        workspace,
        first_message,
        active_messages=first_messages,
        expected_seq=first_seq,
    )
    second_message = {"role": "user", "content": "stale writer"}
    second_messages.append(second_message)

    with pytest.raises(RuntimeError, match="另一个进程"):
        second.append_message(
            session.session_id,
            workspace,
            second_message,
            active_messages=second_messages,
            expected_seq=second_seq,
        )

    assert first.load_messages(session.session_id, workspace) == [
        {"role": "system", "content": "system"},
        first_message,
    ]


def test_session_lock_serializes_concurrent_appends(
    workspace: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "sessions"
    seed = SessionStore(data_dir)
    session = seed.create(workspace, "system")

    def append(index: int) -> int:
        return SessionStore(data_dir).append_message(
            session.session_id,
            workspace,
            {"role": "user", "content": f"message-{index}"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        sequences = list(pool.map(append, range(12)))

    assert sorted(sequences) == list(range(2, 14))
    assert len(seed.load_messages(session.session_id, workspace)) == 13


def test_active_recovery_applies_byte_limit_to_journal_tail(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "sessions"
    store = SessionStore(data_dir)
    session = store.create(workspace, "system")

    def crash_before_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise OSError("checkpoint crash")

    monkeypatch.setattr(store, "_save_active_messages_unlocked", crash_before_checkpoint)
    with pytest.raises(OSError, match="checkpoint crash"):
        store.append_message(
            session.session_id,
            workspace,
            {"role": "user", "content": "x" * 2_000},
        )

    limited = SessionStore(data_dir, max_bytes=500)
    with pytest.raises(ValueError, match="journal 尾部.*字节上限"):
        limited.load_active_messages(session.session_id, workspace)


def test_active_checkpoint_replays_journal_tail_after_interrupted_save(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    original_save = store._save_active_messages_unlocked

    def crash_before_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(store, "_save_active_messages_unlocked", crash_before_checkpoint)
    with pytest.raises(OSError, match="simulated crash"):
        store.append_message(
            session.session_id,
            workspace,
            {"role": "user", "content": "durable tail"},
        )
    monkeypatch.setattr(store, "_save_active_messages_unlocked", original_save)

    recovered = store.load_active_messages(session.session_id, workspace)
    assert recovered[-1] == {"role": "user", "content": "durable tail"}


def test_recovery_ignores_partial_final_line_and_repairs_interrupted_tool_call(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    original_save = store._save_active_messages_unlocked
    assistant_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "interrupted",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }
        ],
    }

    def crash_before_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash")

    monkeypatch.setattr(store, "_save_active_messages_unlocked", crash_before_checkpoint)
    with pytest.raises(OSError):
        store.append_message(session.session_id, workspace, assistant_call)
    monkeypatch.setattr(store, "_save_active_messages_unlocked", original_save)

    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    journal = sorted(session_dir.glob("context*.jsonl"))[-1]
    with journal.open("ab") as stream:
        stream.write(b'{"seq":999')

    recovered = store.load_active_messages(
        session.session_id,
        workspace,
        repair_incomplete=True,
    )
    synthetic = recovered[-1]
    assert synthetic["role"] == "tool"
    assert synthetic["tool_call_id"] == "interrupted"
    result = json.loads(synthetic["content"])
    assert result["ok"] is False
    assert result["metadata"]["synthetic_recovery"] is True


def test_journal_rejects_complete_corrupt_line(
    workspace: Path,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    journal = sorted(session_dir.glob("context*.jsonl"))[-1]
    with journal.open("ab") as stream:
        stream.write(b"not-json\n")

    with pytest.raises(ValueError, match="不是 JSON"):
        store.load_messages(session.session_id, workspace)


def test_session_rejects_symlinked_active_context(
    workspace: Path,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    active = session_dir / "active_context.json"
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    active.unlink()
    active.symlink_to(outside)

    with pytest.raises(ValueError, match="符号链接"):
        store.load_active_messages(session.session_id, workspace)


def test_repl_resume_rejects_cross_provider_switch(
    workspace: Path,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    current = store.create(workspace, "system", provider="scripted")
    target = store.create(
        workspace,
        "system",
        provider="openai",
        model="model-x",
        base_url="https://example.invalid/v1",
    )
    output = io.StringIO()
    repl = CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        provider_name="scripted",
        store=store,
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        session_id=current.session_id,
        output=output,
        interactive_approvals=False,
    )

    assert repl._handle_command(f"/resume {target.session_id}") is True
    assert repl.session.session_id == current.session_id
    assert "使用不同的 Provider" in output.getvalue()
    assert "--provider openai" in output.getvalue()


def test_repl_labels_unverified_final_as_candidate(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.StringIO()
    repl = CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=output,
        interactive_approvals=False,
    )
    monkeypatch.setattr(
        repl.agent,
        "run",
        lambda *_args, **_kwargs: RunResult(
            RunStatus.UNVERIFIED,
            "run-id",
            2,
            final_text="修改完成",
            error="缺少修改后的验证证据",
        ),
    )

    assert repl.run_turn("修改文件") == 1
    rendered = output.getvalue()
    assert "candidate(unverified)> 修改完成" in rendered
    assert "assistant> 修改完成" not in rendered
    assert "运行未完成：缺少修改后的验证证据" in rendered
