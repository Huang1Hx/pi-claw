"""SessionRepository 契约测试：同一组用例必须对 files 与 sqlite 后端等价成立。

M0 阶段只有 files 后端；M3 增加 sqlite 时只需扩展 BACKENDS 列表，无需改写用例。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from core.active_context import ActiveContext, Pin, TaskAnchor
from persistence.repository import SessionRepository
from persistence.session_store import SessionStore
from persistence.sqlite_repository import SqliteSessionRepository

RepositoryFactory = Callable[[], SessionRepository]

BACKENDS = ("files", "sqlite")


@pytest.fixture(params=BACKENDS)
def repo_factory(request: pytest.FixtureRequest, tmp_path: Path) -> RepositoryFactory:
    """按后端名返回仓库构造函数；增加后端时只需扩展 BACKENDS。"""
    name = request.param
    if name == "files":
        return lambda: SessionStore(tmp_path / "sessions")
    if name == "sqlite":
        return lambda: SqliteSessionRepository(tmp_path / "sessions.sqlite3")
    raise AssertionError(f"未知后端：{name}")


def test_store_implements_repository_protocol(repo_factory: RepositoryFactory) -> None:
    assert isinstance(repo_factory(), SessionRepository)


def test_create_append_load_and_list(repo_factory: RepositoryFactory, workspace: Path) -> None:
    repo = repo_factory()
    session = repo.create(workspace, "system prompt")
    appended = repo.append_message(session.session_id, workspace, {"role": "user", "content": "hi"})
    assert appended == 2

    assert repo.load(session.session_id, workspace).session_id == session.session_id
    assert repo.load_messages(session.session_id, workspace) == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hi"},
    ]
    assert [item.session_id for item in repo.list_sessions(workspace)] == [session.session_id]
    assert repo.latest(workspace).session_id == session.session_id  # type: ignore[union-attr]


def test_stale_writer_is_rejected(repo_factory: RepositoryFactory, workspace: Path) -> None:
    """两个持有同一 seq 的写入者只有一个能提交，另一个必须显式失败。"""
    first, second = repo_factory(), repo_factory()
    session = first.create(workspace, "system")
    _messages, seq = first.load_active_snapshot(session.session_id, workspace)
    first.append_message(
        session.session_id, workspace, {"role": "user", "content": "winner"}, expected_seq=seq
    )
    with pytest.raises(RuntimeError, match="另一个进程"):
        second.append_message(
            session.session_id, workspace, {"role": "user", "content": "loser"}, expected_seq=seq
        )


def test_active_context_round_trips_pins_and_anchor(
    repo_factory: RepositoryFactory, workspace: Path
) -> None:
    repo = repo_factory()
    session = repo.create(workspace, "system")
    messages, seq = repo.load_active_snapshot(session.session_id, workspace)
    context = ActiveContext(
        messages=messages,
        through_seq=seq,
        snapshot_revision=1,
        pins=(Pin(pin_id="p1", text="只读调查", seq=seq),),
        anchor=TaskAnchor.build(text="目标：修复回归", covered_through_seq=seq, sources=("task",)),
    )
    repo.save_active_context(session.session_id, workspace, context)

    loaded = repo.load_active_context(session.session_id, workspace)
    assert loaded.through_seq == seq
    assert loaded.messages == messages
    assert [pin.text for pin in loaded.pins] == ["只读调查"]
    assert loaded.anchor is not None and loaded.anchor.text == "目标：修复回归"


def test_message_save_keeps_pins_and_anchor(
    repo_factory: RepositoryFactory, workspace: Path
) -> None:
    repo = repo_factory()
    session = repo.create(workspace, "system")
    messages, seq = repo.load_active_snapshot(session.session_id, workspace)
    context = ActiveContext(
        messages=messages,
        through_seq=seq,
        pins=(Pin(pin_id="p1", text="钉住约束", seq=seq),),
    )
    repo.save_active_context(session.session_id, workspace, context)

    compacted = messages + [{"role": "assistant", "content": "summary"}]
    repo.save_active_messages(session.session_id, workspace, compacted)

    loaded = repo.load_active_context(session.session_id, workspace)
    assert loaded.messages == compacted
    assert [pin.text for pin in loaded.pins] == ["钉住约束"]


def test_usage_is_idempotent_per_run(repo_factory: RepositoryFactory, workspace: Path) -> None:
    repo = repo_factory()
    session = repo.create(workspace, "system")
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "requests": 1,
        "requests_with_usage": 1,
        "requests_without_usage": 0,
        "retries": 0,
    }
    repo.record_usage(session.session_id, workspace, "run-1", usage)
    repo.record_usage(session.session_id, workspace, "run-1", usage)

    assert repo.total_usage(session.session_id, workspace)["total_tokens"] == 15
    assert repo.workspace_usage(workspace)["totals"]["total_tokens"] == 15


def test_update_state_and_runtime(repo_factory: RepositoryFactory, workspace: Path) -> None:
    repo = repo_factory()
    session = repo.create(workspace, "system")
    titled = repo.update_state(session.session_id, workspace, title="focused", approved_tools=["a"])
    assert titled.title == "focused" and titled.approved_tools == ["a"]
    updated = repo.update_runtime(
        session.session_id, workspace, provider="routed", model="m", base_url=None
    )
    assert updated.provider == "routed" and updated.model == "m"
