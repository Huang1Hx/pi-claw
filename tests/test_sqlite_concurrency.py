"""M4：真实多进程竞争与进程崩溃下的 SQLite 耐久性。

这些测试用真实子进程制造竞争与 kill，属于**进程崩溃**验证，不等价于断电或硬件故障。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from persistence.sqlite_repository import SqliteSessionRepository

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _db(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


def make_repo(tmp_path: Path, **kwargs) -> SqliteSessionRepository:
    return SqliteSessionRepository(_db(tmp_path), **kwargs)


def _spawn(script: str, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _result(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=30)
    lines = [line for line in out.strip().splitlines() if line.strip()]
    assert lines, f"worker 无输出；stderr={err}"
    return json.loads(lines[-1])


APPEND_WORKER = """
import json, sys
from persistence.sqlite_repository import SqliteSessionRepository
db, ws, sid, seq, tag = sys.argv[1:6]
repo = SqliteSessionRepository(db)
try:
    repo.append_message(sid, ws, {"role": "user", "content": tag}, expected_seq=int(seq))
    print(json.dumps({"ok": True}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
"""

SNAPSHOT_WORKER = """
import json, sys
from persistence.sqlite_repository import SqliteSessionRepository
from core.active_context import Pin
db, ws, sid, rev, tag = sys.argv[1:6]
repo = SqliteSessionRepository(db)
ctx = repo.load_active_context(sid, ws)
try:
    repo.save_active_context(
        sid, ws, ctx.with_pins((Pin(pin_id=tag, text=tag, seq=ctx.through_seq),)),
        expected_revision=int(rev),
    )
    print(json.dumps({"ok": True}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
"""

KILL_TXN_WORKER = """
import os, signal, sqlite3, sys
db, sid, mode = sys.argv[1:4]
conn = sqlite3.connect(db, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO events(session_id, seq, message, created_at) VALUES(?,?,?,?)",
    (sid, 2, '{"role": "user", "content": "ghost"}', "t"),
)
if mode == "commit":
    conn.execute("COMMIT")
os.kill(os.getpid(), signal.SIGKILL)
"""

HOLD_LOCK_WORKER = """
import sqlite3, sys, time
db, seconds = sys.argv[1:3]
conn = sqlite3.connect(db, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
time.sleep(float(seconds))
conn.execute("ROLLBACK")
"""


def test_multiprocess_append_race_single_winner(tmp_path, workspace):
    """两个进程基于同一 seq 追加：只有一个提交，另一个显式失败，无静默覆盖。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    _messages, seq = repo.load_active_snapshot(session.session_id, workspace)

    first = _spawn(
        APPEND_WORKER, str(_db(tmp_path)), str(workspace), session.session_id, str(seq), "A"
    )
    second = _spawn(
        APPEND_WORKER, str(_db(tmp_path)), str(workspace), session.session_id, str(seq), "B"
    )
    results = [_result(first), _result(second)]

    assert sum(1 for item in results if item["ok"]) == 1
    assert sum(1 for item in results if not item["ok"]) == 1
    messages = repo.load_messages(session.session_id, workspace)
    assert len(messages) == 2  # system + 唯一胜者
    assert messages[-1]["content"] in {"A", "B"}


def test_multiprocess_snapshot_only_race_single_winner(tmp_path, workspace):
    """相同 seq 的仅快照竞争：修订号 CAS 只允许一个提交。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    revision = repo.load_active_context(session.session_id, workspace).snapshot_revision

    first = _spawn(
        SNAPSHOT_WORKER, str(_db(tmp_path)), str(workspace), session.session_id, str(revision), "p1"
    )
    second = _spawn(
        SNAPSHOT_WORKER, str(_db(tmp_path)), str(workspace), session.session_id, str(revision), "p2"
    )
    results = [_result(first), _result(second)]

    assert sum(1 for item in results if item["ok"]) == 1
    pins = repo.load_active_context(session.session_id, workspace).pins
    assert [pin.pin_id for pin in pins] in (["p1"], ["p2"])


def test_kill_during_uncommitted_write_rolls_back(tmp_path, workspace):
    """未提交事务中 kill：重开后回到完整旧状态，无半写。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    worker = _spawn(KILL_TXN_WORKER, str(_db(tmp_path)), session.session_id, "abort")
    assert worker.wait(timeout=30) == -signal.SIGKILL

    reopened = make_repo(tmp_path)
    assert reopened.integrity_check() == "ok"
    assert reopened.load_messages(session.session_id, workspace) == [
        {"role": "system", "content": "system"}
    ]
    assert reopened.load_active_snapshot(session.session_id, workspace)[1] == 1
    # 仍可正常写入，序号从 2 继续。
    assert (
        reopened.append_message(session.session_id, workspace, {"role": "user", "content": "next"})
        == 2
    )


def test_kill_after_commit_preserves_data(tmp_path, workspace):
    """已提交事务后 kill：重开后数据仍在（已确认提交不丢）。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    worker = _spawn(KILL_TXN_WORKER, str(_db(tmp_path)), session.session_id, "commit")
    assert worker.wait(timeout=30) == -signal.SIGKILL

    reopened = make_repo(tmp_path)
    assert reopened.integrity_check() == "ok"
    messages = reopened.load_messages(session.session_id, workspace)
    assert messages[-1] == {"role": "user", "content": "ghost"}


def test_integrity_check_and_file_permissions(tmp_path, workspace):
    """库文件及其 WAL/SHM 收紧为 0600；integrity_check 为 ok。"""
    repo = make_repo(tmp_path)
    session = repo.create(workspace, "system")
    repo.append_message(session.session_id, workspace, {"role": "user", "content": "x"})

    assert repo.integrity_check() == "ok"
    checked = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{_db(tmp_path)}{suffix}")
        if candidate.exists():
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
            checked += 1
    assert checked >= 1


def test_write_lock_timeout_fails_explicitly(tmp_path, workspace):
    """写锁被长时间占用时按 busy_timeout 显式失败，而不是无限阻塞。"""
    repo = make_repo(tmp_path, busy_timeout_ms=300)
    session = repo.create(workspace, "system")
    holder = _spawn(HOLD_LOCK_WORKER, str(_db(tmp_path)), "3")
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        repo.append_message(session.session_id, workspace, {"role": "user", "content": "x"})
    assert time.monotonic() - started < 2.5
    holder.wait(timeout=30)
