"""M6b：双后端下命令行为、CLI 后端切换、迁移接线与故障反馈。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from core.schemas import ModelReply
from interfaces.cli import main
from interfaces.repl import CodingRepl
from persistence.factory import open_session_store
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider

BACKENDS = ("files", "sqlite")


def make_repl(tmp_path: Path, backend: str, *, continue_latest: bool = False) -> CodingRepl:
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=ScriptedProvider([ModelReply.final("done")]),
        output=io.StringIO(),
        store=open_session_store(tmp_path / "sessions", backend=backend),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        continue_latest=continue_latest,
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_context_reports_actual_backend(tmp_path: Path, backend: str) -> None:
    repl = make_repl(tmp_path, backend)
    assert repl._handle_command("/context")
    assert f"会话后端：{backend}" in repl.output.getvalue()


@pytest.mark.parametrize("backend", BACKENDS)
def test_pin_and_goal_persist_across_reload(tmp_path: Path, backend: str) -> None:
    repl = make_repl(tmp_path, backend)
    assert repl._handle_command("/pin 不要改动评分器")
    assert repl._handle_command("/goal 修复登录回归")

    reloaded = make_repl(tmp_path, backend, continue_latest=True)
    active = reloaded.load_context()
    assert [pin.text for pin in active.pins] == ["不要改动评分器"]
    assert active.goal == "修复登录回归"


def test_cli_sessions_flag_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system", provider="scripted")

    def run(env_backend: str | None, flag: str | None) -> str:
        if env_backend is None:
            monkeypatch.delenv("PI_CLAW_SESSION_BACKEND", raising=False)
        else:
            monkeypatch.setenv("PI_CLAW_SESSION_BACKEND", env_backend)
        argv = ["sessions", "--workspace", str(workspace), "--data-dir", str(tmp_path / "sessions")]
        if flag is not None:
            argv += ["--session-backend", flag]
        return _capture(argv, tmp_path / "out")

    # env=sqlite 时文件后端会话不可见；显式 --session-backend files 覆盖 env 后又可见。
    assert session.session_id not in run("sqlite", None)
    assert session.session_id in run("sqlite", "files")


def test_cli_migrate_and_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    files = SessionStore(tmp_path / "sessions")
    session = files.create(workspace, "system", provider="scripted")
    files.append_message(session.session_id, workspace, {"role": "user", "content": "hello"})

    trace = tmp_path / "migrate.jsonl"
    out = _capture(
        [
            "migrate",
            "--workspace",
            str(workspace),
            "--from",
            "files",
            "--to",
            "sqlite",
            "--data-dir",
            str(tmp_path / "sessions"),
            "--trace",
            str(trace),
        ],
        tmp_path / "out",
    )
    assert "迁移完成" in out

    sqlite = open_session_store(tmp_path / "sessions", backend="sqlite")
    assert [i.session_id for i in sqlite.list_sessions(workspace)] == [session.session_id]
    # 源文件后端保持不变。
    assert [i.session_id for i in files.list_sessions(workspace)] == [session.session_id]
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    migrated = [e for e in events if e["event"] == "session_migrated"]
    assert len(migrated) == 1
    assert migrated[0]["payload"]["to_backend"] == "sqlite"
    assert migrated[0]["payload"]["imported"] == 1


def test_migration_conflict_fails_without_partial_acceptance(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    files = SessionStore(tmp_path / "sessions")
    session = files.create(workspace, "system", provider="scripted")
    sqlite = open_session_store(tmp_path / "sessions", backend="sqlite")
    # 目标库中预置同 id 但不同 created_at 的会话，迁移必须在写入前显式失败。
    conflicting = replace(
        files.load(session.session_id, workspace), created_at="2000-01-01T00:00:00Z"
    )
    sqlite.import_session(
        conflicting,
        [{"role": "system", "content": "system"}],
        files.load_active_context(session.session_id, workspace),
        {},
    )
    sqlite.close()
    trace = tmp_path / "migrate.jsonl"
    code = _capture_code(
        [
            "migrate",
            "--workspace",
            str(workspace),
            "--from",
            "files",
            "--to",
            "sqlite",
            "--data-dir",
            str(tmp_path / "sessions"),
            "--trace",
            str(trace),
        ],
        tmp_path / "out",
    )
    assert code == 2
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "session_migration_failed" for e in events)


def test_real_repl_sqlite_rows_are_independently_readable(tmp_path: Path) -> None:
    """真实 REPL 落到 SQLite 后，用独立 sqlite3 连接直读原始行核对，不靠同源 API 自证。"""
    repl = make_repl(tmp_path, "sqlite")
    assert repl._handle_command("/goal 修复登录回归")
    secret = "sk-ABCDEFGH12345678"
    assert repl._handle_command(f"/pin 不要改动评分器 key={secret}")
    repl.run_turn("查看项目结构")
    repl.store.close()

    db_path = tmp_path / "sessions" / "sessions.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = conn.execute(
            "SELECT through_seq, context FROM snapshots WHERE session_id=?",
            (repl.session.session_id,),
        ).fetchone()
        assert snapshot is not None
        payload = json.loads(snapshot["context"])
        assert payload["goal"] == "修复登录回归"
        assert "不要改动评分器" in payload["pins"][0]["text"]
        # 凭据在落库前已脱敏，独立连接也读不到原文。
        assert secret not in snapshot["context"]
        max_seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?",
            (repl.session.session_id,),
        ).fetchone()[0]
        assert payload["through_seq"] == max_seq == snapshot["through_seq"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_cli_chat_sqlite_session_independently_readable(tmp_path: Path) -> None:
    """通过真实 CLI 进程 + 环境变量选中 SQLite，再用独立连接核对落库结果。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data_dir = tmp_path / "sessions"
    env = os.environ.copy()
    env["PI_CLAW_SESSION_BACKEND"] = "sqlite"
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        env.pop(name, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "interfaces",
            "chat",
            "--provider",
            "scripted",
            "--print",
            "--data-dir",
            str(data_dir),
            "列出项目结构",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    db_path = data_dir / "sessions.sqlite3"
    assert db_path.exists()
    assert (db_path.stat().st_mode & 0o077) == 0  # 数据文件对同组/其他用户不可读写
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT through_seq, context FROM snapshots").fetchone()
        assert row is not None
        payload = json.loads(row["context"])
        max_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        assert payload["version"] == 3
        assert payload["through_seq"] == max_seq
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# --- helpers ---


def _capture(argv: list[str], out_dir: Path) -> str:
    import contextlib

    out_dir.mkdir(exist_ok=True)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    assert code == 0, buffer.getvalue()
    return buffer.getvalue()


def _capture_code(argv: list[str], out_dir: Path) -> int:
    import contextlib

    out_dir.mkdir(exist_ok=True)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code
