"""M3：SQLite 后端专项保证——事务原子性、工作区隔离、CAS 与跨实例恢复。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.active_context import Pin
from persistence.sqlite_repository import SqliteSessionRepository


def make_repo(tmp_path: Path, **kwargs) -> SqliteSessionRepository:
    return SqliteSessionRepository(tmp_path / "sessions.sqlite3", **kwargs)


def test_append_failure_rolls_back_without_partial_event(tmp_path, workspace):
    """追加在预算校验失败时必须整体回滚：不得留下半条事件或推进序号。"""
    repo = make_repo(tmp_path, max_events=2)
    session = repo.create(workspace, "system")
    with pytest.raises(ValueError, match="消息上限"):
        repo.append_message(
            session.session_id,
            workspace,
            {"role": "user", "content": "boom"},
            active_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"},
            ],
        )
    assert repo.load_messages(session.session_id, workspace) == [
        {"role": "system", "content": "system"}
    ]
    assert repo.load_active_snapshot(session.session_id, workspace)[1] == 1


def test_workspace_isolation(tmp_path):
    """不同工作区互不读取：列表不含对方，按错误工作区加载显式失败。"""
    repo = make_repo(tmp_path)
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    session = repo.create(first, "system")

    assert [info.session_id for info in repo.list_sessions(second)] == []
    with pytest.raises(ValueError, match="工作区"):
        repo.load(session.session_id, second)
    with pytest.raises(ValueError, match="工作区"):
        repo.load_active_context(session.session_id, second)
    with pytest.raises(ValueError):
        repo.record_usage(session.session_id, second, "run-1", {"prompt_tokens": 1})


def test_revision_cas_rejects_stale_snapshot_update(tmp_path, workspace):
    """同修订号的两次原地更新只有一个提交，另一个显式失败。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    first = repo.load_active_context(session.session_id, workspace)
    second = repo.load_active_context(session.session_id, workspace)

    repo.save_active_context(
        session.session_id,
        workspace,
        first.with_pins((Pin(pin_id="p1", text="one", seq=first.through_seq),)),
        expected_revision=first.snapshot_revision,
    )
    with pytest.raises(RuntimeError, match="另一个进程"):
        repo.save_active_context(
            session.session_id,
            workspace,
            second.with_pins((Pin(pin_id="p2", text="two", seq=second.through_seq),)),
            expected_revision=second.snapshot_revision,
        )
    loaded = repo.load_active_context(session.session_id, workspace)
    assert [pin.pin_id for pin in loaded.pins] == ["p1"]


def test_state_survives_new_instance(tmp_path, workspace):
    """关闭并重开：事件、快照(pins/anchor)、用量在事务提交后完整可见。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    messages, seq = repo.load_active_snapshot(session.session_id, workspace)
    context = repo.load_active_context(session.session_id, workspace).with_pins(
        (Pin(pin_id="p1", text="保留约束", seq=seq),)
    )
    repo.save_active_context(
        session.session_id, workspace, context, expected_revision=context.snapshot_revision - 1
    )
    repo.append_message(session.session_id, workspace, {"role": "user", "content": "hi"})
    repo.record_usage(
        session.session_id,
        workspace,
        "run-1",
        {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "requests": 1,
            "requests_with_usage": 1,
            "requests_without_usage": 0,
            "retries": 0,
        },
    )
    repo.close()

    reopened = make_repo(tmp_path)
    loaded = reopened.load_active_context(session.session_id, workspace)
    assert [pin.text for pin in loaded.pins] == ["保留约束"]
    assert loaded.messages[-1] == {"role": "user", "content": "hi"}
    assert reopened.total_usage(session.session_id, workspace)["total_tokens"] == 5


def test_unknown_snapshot_version_fails_closed_without_write(tmp_path, workspace):
    """未知快照版本必须显式失败，且不得写入新事件。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    repo._conn.execute(
        "UPDATE snapshots SET context=? WHERE session_id=?",
        (json.dumps({"version": 99, "through_seq": 1, "messages": []}), session.session_id),
    )
    with pytest.raises(ValueError, match="版本"):
        repo.load_active_context(session.session_id, workspace)
    with pytest.raises(ValueError, match="版本"):
        repo.append_message(session.session_id, workspace, {"role": "user", "content": "x"})
    assert len(repo.load_messages(session.session_id, workspace)) == 1


def test_update_runtime_rejects_credentials_in_url(tmp_path, workspace):
    """API 地址不得夹带凭据或查询参数。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    with pytest.raises(ValueError):
        repo.update_runtime(
            session.session_id,
            workspace,
            provider="routed",
            model="m",
            base_url="https://gw.example/v1?api_key=secret",
        )


def test_usage_is_idempotent_and_aggregates(tmp_path, workspace):
    """同 run_id 只累计一次；跨会话聚合按工作区。"""
    repo = make_repo(tmp_path)
    first = repo.create(workspace, "system")
    second = repo.create(workspace, "system")
    usage = {
        "prompt_tokens": 4,
        "completion_tokens": 1,
        "total_tokens": 5,
        "requests": 1,
        "requests_with_usage": 1,
        "requests_without_usage": 0,
        "retries": 0,
    }
    repo.record_usage(first.session_id, workspace, "r1", usage)
    repo.record_usage(first.session_id, workspace, "r1", usage)
    repo.record_usage(second.session_id, workspace, "r2", usage)

    assert repo.total_usage(first.session_id, workspace)["total_tokens"] == 5
    report = repo.workspace_usage(workspace)
    assert report["totals"]["total_tokens"] == 10
    assert report["recorded_sessions"] == 2


def test_snapshot_save_preserves_goal_and_anchor(tmp_path, workspace):
    """完整上下文保存保留 goal/anchor；消息兼容保存不擦除它们。"""
    from core.active_context import TaskAnchor

    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    context = repo.load_active_context(session.session_id, workspace)
    updated = context.with_goal("修复回归").with_anchor(
        TaskAnchor.build(text="目标：修复回归", covered_through_seq=0, sources=("task",))
    )
    repo.save_active_context(
        session.session_id, workspace, updated, expected_revision=context.snapshot_revision
    )
    repo.save_active_messages(
        session.session_id, workspace, [{"role": "system", "content": "system"}]
    )
    loaded = repo.load_active_context(session.session_id, workspace)
    assert loaded.goal == "修复回归"
    assert loaded.anchor is not None and loaded.anchor.text == "目标：修复回归"
