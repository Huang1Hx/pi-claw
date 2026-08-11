from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from pi_claw.providers import OfflineCodingProvider
from pi_claw.repl import CodingRepl
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
    event_store = SessionStore(tmp_path / "event-sessions", max_events=1)
    event_session = event_store.create(workspace, "system")
    event_store.append_message(
        event_session.session_id, workspace, {"role": "user", "content": "second"}
    )
    with pytest.raises(ValueError, match="消息上限"):
        event_store.load_messages(event_session.session_id, workspace)

    byte_store = SessionStore(tmp_path / "byte-sessions", max_bytes=20)
    byte_session = byte_store.create(workspace, "system message larger than limit")
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
