"""M6b：会话后端工厂的选择优先级、路径与故障语义。"""

from __future__ import annotations

from pathlib import Path

import pytest

from persistence.factory import (
    ENV_BACKEND,
    default_sessions_dir,
    open_session_store,
    resolve_backend,
    store_backend,
)
from persistence.session_store import SessionStore
from persistence.sqlite_repository import SqliteSessionRepository


def test_default_backend_is_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_BACKEND, raising=False)
    monkeypatch.setenv("PI_CLAW_HOME", str(tmp_path / "home"))
    store = open_session_store(tmp_path / "sessions")
    assert isinstance(store, SessionStore)
    assert store_backend(store) == "files"


def test_env_selects_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_BACKEND, "sqlite")
    store = open_session_store(tmp_path / "sessions")
    assert isinstance(store, SqliteSessionRepository)
    assert store_backend(store) == "sqlite"
    store.close()


def test_explicit_backend_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_BACKEND, "sqlite")
    store = open_session_store(tmp_path / "sessions", backend="files")
    assert isinstance(store, SessionStore)
    assert resolve_backend("files", env={ENV_BACKEND: "sqlite"}) == "files"
    assert resolve_backend(None, env={ENV_BACKEND: "sqlite"}) == "sqlite"
    assert resolve_backend(None, env={}) == "files"


def test_unknown_backend_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_BACKEND, raising=False)
    with pytest.raises(ValueError):
        open_session_store(tmp_path / "sessions", backend="postgres")
    with pytest.raises(ValueError):
        resolve_backend(None, env={ENV_BACKEND: "redis"})


def test_sqlite_file_lives_under_data_dir(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = open_session_store(root, backend="sqlite")
    assert isinstance(store, SqliteSessionRepository)
    assert store.path == (root / "sessions.sqlite3").resolve()
    store.close()


def test_default_sessions_dir_respects_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PI_CLAW_HOME", str(tmp_path / "custom"))
    assert default_sessions_dir() == tmp_path / "custom" / "sessions"


def test_sqlite_failure_never_falls_back_to_files(tmp_path: Path) -> None:
    """数据目录被同名文件占用时，SQLite 后端必须报错而不是退回文件后端。"""
    blocked = tmp_path / "sessions"
    blocked.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        open_session_store(blocked, backend="sqlite")


def test_sqlite_store_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = open_session_store(tmp_path / "sessions", backend="sqlite")
    session = store.create(workspace, "system prompt", provider="scripted")
    store.append_message(session.session_id, workspace, {"role": "user", "content": "hello"})
    reloaded = open_session_store(tmp_path / "sessions", backend="sqlite")
    assert reloaded.load(session.session_id, workspace).session_id == session.session_id
    assert [m["content"] for m in reloaded.load_messages(session.session_id, workspace)] == [
        "system prompt",
        "hello",
    ]
    store.close()
    reloaded.close()
