"""会话后端工厂：按 CLI 参数 / 环境变量 / 默认值选择文件或 SQLite 后端。

选择优先级固定为「显式参数 > 环境变量 > 默认 files」。未知取值立即报错，绝不静默回退，
因为静默回退会让用户以为数据写进了 SQLite，实际却留在文件后端，或反之。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from persistence.repository import SessionRepository
from persistence.session_store import SessionStore
from persistence.sqlite_repository import SqliteSessionRepository

ENV_BACKEND = "PI_CLAW_SESSION_BACKEND"
DEFAULT_BACKEND = "files"
SQLITE_FILENAME = "sessions.sqlite3"
SUPPORTED_BACKENDS = ("files", "sqlite")


def default_sessions_dir() -> Path:
    """返回与 SessionStore 一致的默认会话根目录（PI_CLAW_HOME 优先）。"""
    configured = os.environ.get("PI_CLAW_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".pi-claw"
    return home / "sessions"


def resolve_backend(
    backend: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """解析后端名；显式参数优先于环境变量，空值视为未设置。"""
    source: Mapping[str, str] = os.environ if env is None else env
    selected = backend if backend is not None and str(backend).strip() else source.get(ENV_BACKEND)
    if selected is None or not str(selected).strip():
        return DEFAULT_BACKEND
    name = str(selected).strip().lower()
    if name not in SUPPORTED_BACKENDS:
        raise ValueError(f"未知会话后端：{selected}；可选 {', '.join(SUPPORTED_BACKENDS)}")
    return name


def open_session_store(
    data_dir: str | Path | None = None,
    *,
    backend: str | None = None,
) -> SessionRepository:
    """按解析出的后端构造仓储；SQLite 库文件固定落在数据目录下的单文件。"""
    name = resolve_backend(backend)
    if name == "files":
        return SessionStore(data_dir)
    root = Path(data_dir) if data_dir is not None else default_sessions_dir()
    root = root.expanduser().resolve(strict=False)
    return SqliteSessionRepository(root / SQLITE_FILENAME)


def store_backend(store: object) -> str:
    """识别仓储实例对应的后端名，供 /context、/status 与 Trace 如实报告。"""
    if isinstance(store, SqliteSessionRepository):
        return "sqlite"
    if isinstance(store, SessionStore):
        return "files"
    return "unknown"
