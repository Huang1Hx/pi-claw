"""M5：files ↔ sqlite 迁移的往返、幂等、冲突与中断可恢复性。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.active_context import ActiveContext, Pin, TaskAnchor
from persistence.migrate import MigrationConflict, migrate_workspace
from persistence.session_store import SessionStore
from persistence.sqlite_repository import SqliteSessionRepository

USAGE = {
    "prompt_tokens": 9,
    "completion_tokens": 4,
    "total_tokens": 13,
    "requests": 1,
    "requests_with_usage": 1,
    "requests_without_usage": 0,
    "retries": 0,
}


def files_repo(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "files")


def sqlite_repo(tmp_path: Path) -> SqliteSessionRepository:
    return SqliteSessionRepository(tmp_path / "sessions.sqlite3")


def seed_files(tmp_path: Path, workspace: Path, *, title: str, goal: str, content: str) -> str:
    """在 files 后端建一个有代表性的会话：消息、goal、pins、锚、用量、元数据。"""
    repo = files_repo(tmp_path)
    session = repo.create(workspace, "system prompt", provider="routed", model="deepseek-flash")
    repo.append_message(session.session_id, workspace, {"role": "user", "content": content})
    repo.append_message(session.session_id, workspace, {"role": "assistant", "content": "收到"})
    context = repo.load_active_context(session.session_id, workspace)
    repo.save_active_context(
        session.session_id,
        workspace,
        context.with_goal(goal)
        .with_pins((Pin(pin_id="p1", text="不要改动评分器", seq=context.through_seq),))
        .with_anchor(
            TaskAnchor.build(
                text=f"目标：{goal}", covered_through_seq=context.through_seq, sources=("task",)
            )
        ),
        expected_revision=context.snapshot_revision,
    )
    repo.update_state(session.session_id, workspace, title=title, approved_tools=["read_file"])
    repo.record_usage(session.session_id, workspace, "run-1", USAGE)
    return session.session_id


def normalized(repo, session_id: str, workspace: Path) -> dict:
    info = repo.load(session_id, workspace)
    return {
        "session_id": info.session_id,
        "created_at": info.created_at,
        "title": info.title,
        "provider": info.provider,
        "model": info.model,
        "base_url": info.base_url,
        "approved_tools": info.approved_tools,
        "messages": repo.load_messages(session_id, workspace),
        "context": repo.load_active_context(session_id, workspace).to_payload(),
        "usage": repo.usage_records(session_id, workspace),
    }


def test_files_to_sqlite_round_trip_equality(tmp_path, workspace):
    sid = seed_files(tmp_path, workspace, title="计费", goal="修复计费回归", content="开始")
    source = files_repo(tmp_path)
    target = sqlite_repo(tmp_path)

    report = migrate_workspace(source, target, workspace)
    assert report.total == 1 and report.imported == 1 and report.skipped == 0
    assert normalized(target, sid, workspace) == normalized(source, sid, workspace)


def test_repeat_migration_is_idempotent(tmp_path, workspace):
    sid = seed_files(tmp_path, workspace, title="计费", goal="修复计费回归", content="开始")
    source = files_repo(tmp_path)
    target = sqlite_repo(tmp_path)

    migrate_workspace(source, target, workspace)
    again = migrate_workspace(source, target, workspace)
    assert again.imported == 0 and again.skipped == 1
    assert normalized(target, sid, workspace) == normalized(source, sid, workspace)
    assert len(target.load_messages(sid, workspace)) == 3


def test_reverse_migration_sqlite_to_files(tmp_path, workspace):
    sid = seed_files(tmp_path, workspace, title="计费", goal="修复计费回归", content="开始")
    forward = sqlite_repo(tmp_path)
    migrate_workspace(files_repo(tmp_path), forward, workspace)

    back = SessionStore(tmp_path / "restored")
    report = migrate_workspace(forward, back, workspace)
    assert report.imported == 1
    assert normalized(back, sid, workspace) == normalized(forward, sid, workspace)


def test_conflict_reports_before_any_write(tmp_path, workspace):
    keep = seed_files(tmp_path, workspace, title="A", goal="目标A", content="a")
    seed_files(tmp_path, workspace, title="B", goal="目标B", content="b")
    source = files_repo(tmp_path)
    target = sqlite_repo(tmp_path)

    # 预先在目标放一个同名但 created_at 不同的会话，制造冲突。
    info = source.load(keep, workspace)
    target.import_session(
        replace(info, created_at="1999-01-01T00:00:00Z"), [], ActiveContext.empty(), {}
    )

    with pytest.raises(MigrationConflict):
        migrate_workspace(source, target, workspace)
    # 预检失败不得写入其它会话。
    assert [i.session_id for i in target.list_sessions(workspace)] == [keep]


def test_interrupted_migration_is_resumable(tmp_path, workspace, monkeypatch):
    first = seed_files(tmp_path, workspace, title="A", goal="目标A", content="a")
    second = seed_files(tmp_path, workspace, title="B", goal="目标B", content="b")
    source = files_repo(tmp_path)
    target = sqlite_repo(tmp_path)

    original = target.import_session
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("模拟导入中断")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, "import_session", flaky)
    with pytest.raises(OSError):
        migrate_workspace(source, target, workspace)

    # 源数据完好；已导入的会话是完整的（顺序按 updated_at，故不假定具体 id）。
    assert len(source.load_messages(first, workspace)) == 3
    migrated = [item.session_id for item in target.list_sessions(workspace)]
    assert len(migrated) == 1
    assert normalized(target, migrated[0], workspace) == normalized(source, migrated[0], workspace)

    monkeypatch.setattr(target, "import_session", original)
    report = migrate_workspace(source, target, workspace)
    assert report.total == 2 and report.imported == 1 and report.skipped == 1
    assert normalized(target, second, workspace) == normalized(source, second, workspace)
    assert normalized(target, first, workspace) == normalized(source, first, workspace)


def test_corrupt_source_fails_without_touching_target(tmp_path, workspace):
    """源会话快照损坏时必须显式失败，且不得向目标写入任何会话。"""
    sid = seed_files(tmp_path, workspace, title="A", goal="目标A", content="a")
    source = files_repo(tmp_path)
    session_dir = next(source.data_dir.glob(f"*/{sid}"))
    (session_dir / "active_context.json").write_text(
        '{"version": 99, "through_seq": 1, "messages": []}', encoding="utf-8"
    )
    target = sqlite_repo(tmp_path)

    with pytest.raises(ValueError, match="版本"):
        migrate_workspace(source, target, workspace)
    assert target.list_sessions(workspace) == []
