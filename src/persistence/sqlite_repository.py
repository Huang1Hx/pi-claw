"""SQLite SessionRepository：与文件后端共享调用契约，替换持久化介质。

只替换存储介质，不改上下文压缩、脱敏、权限与审批。凭据、审批与沙箱语义保持一致：
消息/快照写入前脱敏；未知或畸形快照 fail-closed；expected_seq/expected_revision 冲突显式失败。
本模块仍是**内嵌单文件**，不引入外部服务、向量库或分布式依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from core.active_context import ActiveContext, Pin, TaskAnchor
from persistence.checkpoint import CheckpointMixin
from persistence.models import _SESSION_ID_PATTERN, SessionInfo, _now
from persistence.session_store import SessionStore
from persistence.trace import redact
from security.workspace import Workspace

_SCHEMA_VERSION = 1
_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "requests",
    "requests_with_usage",
    "requests_without_usage",
    "retries",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    workspace      TEXT NOT NULL,
    workspace_hash TEXT NOT NULL,
    title          TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT,
    base_url       TEXT,
    approved_tools TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_ws ON sessions(workspace_hash, updated_at DESC);

CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS snapshots (
    session_id  TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    through_seq INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    context     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    session_id            TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id                TEXT NOT NULL,
    prompt_tokens         INTEGER NOT NULL,
    completion_tokens     INTEGER NOT NULL,
    total_tokens          INTEGER NOT NULL,
    requests              INTEGER NOT NULL,
    requests_with_usage   INTEGER NOT NULL,
    requests_without_usage INTEGER NOT NULL,
    retries               INTEGER NOT NULL,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (session_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_ledger(session_id);
"""


class SqliteSessionRepository:
    """用 SQLite 事务实现会话事件、活动快照、元数据与用量的一致性访问。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_events: int = 10_000,
        max_bytes: int = 20_000_000,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if max_events <= 0 or max_bytes <= 0:
            raise ValueError("会话事件与总字节上限必须大于 0")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms 必须是正整数")
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.busy_timeout_ms = busy_timeout_ms
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.execute("PRAGMA synchronous=FULL")
        # executescript 自行管理事务，不能包在 BEGIN IMMEDIATE 里。
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._secure_files()

    # ---- 事务与路径 ----

    class _Txn:
        """上下文管理器：BEGIN IMMEDIATE 起写事务，异常回滚，正常提交。"""

        def __init__(self, repo: SqliteSessionRepository) -> None:
            self._repo = repo
            self._conn = repo._conn

        def __enter__(self) -> sqlite3.Connection:
            self._conn.execute("BEGIN IMMEDIATE")
            return self._conn

        def __exit__(self, exc_type, exc, tb) -> bool:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
            # 每次事务结束都收紧数据库及其 WAL/SHM 文件权限（可能在本事务中新建）。
            self._repo._secure_files()
            return False

    def _write_txn(self) -> _Txn:
        return SqliteSessionRepository._Txn(self)

    def _secure_files(self) -> None:
        """把库文件与其 WAL/SHM 收紧为 0600；缺失文件跳过。"""
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def integrity_check(self) -> str:
        """返回 SQLite `PRAGMA integrity_check` 结果；正常为 "ok"。"""
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    @staticmethod
    def _workspace_root(workspace: str | Path) -> Path:
        return Workspace(workspace).root

    @staticmethod
    def _workspace_hash(root: Path) -> str:
        return hashlib.sha256(os.fsencode(root)).hexdigest()[:24]

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session_id 格式不安全")

    # ---- 会话元数据 ----

    @staticmethod
    def _row_to_info(row: sqlite3.Row) -> SessionInfo:
        return SessionInfo(
            session_id=row["session_id"],
            workspace=row["workspace"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            provider=row["provider"],
            model=row["model"],
            base_url=row["base_url"],
            approved_tools=list(json.loads(row["approved_tools"])),
        )

    def _fetch_info(self, session_id: str, root: Path) -> SessionInfo:
        self._validate_session_id(session_id)
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError("会话不存在或不属于当前工作区")
        info = self._row_to_info(row)
        if Path(info.workspace) != root:
            raise ValueError("会话所属工作区与请求工作区不一致")
        if info.session_id != session_id:
            raise ValueError("会话状态中的 session_id 不一致")
        return info

    def create(
        self,
        workspace: str | Path,
        system_prompt: str,
        *,
        provider: str = "scripted",
        model: str | None = None,
        base_url: str | None = None,
    ) -> SessionInfo:
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("system_prompt 必须是非空字符串")
        SessionStore._validate_runtime_metadata(provider, model, base_url)
        root = self._workspace_root(workspace)
        timestamp = _now()
        session_id = uuid.uuid4().hex
        info = SessionInfo(
            session_id=session_id,
            workspace=str(root),
            title=f"{root.name or root} 会话",
            created_at=timestamp,
            updated_at=timestamp,
            provider=provider,
            model=model,
            base_url=base_url,
            approved_tools=[],
        )
        safe_system = redact({"role": "system", "content": system_prompt})
        context = ActiveContext(
            messages=[safe_system],
            through_seq=1,
            snapshot_revision=0,
            pins=(),
            anchor=None,
            goal="",
        )
        with self._write_txn() as conn:
            conn.execute(
                "INSERT INTO sessions(session_id, workspace, workspace_hash, title, provider,"
                " model, base_url, approved_tools, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    info.session_id,
                    info.workspace,
                    self._workspace_hash(root),
                    info.title,
                    info.provider,
                    info.model,
                    info.base_url,
                    json.dumps(info.approved_tools, ensure_ascii=False),
                    info.created_at,
                    info.updated_at,
                ),
            )
            conn.execute(
                "INSERT INTO events(session_id, seq, message, created_at) VALUES(?,?,?,?)",
                (session_id, 1, json.dumps(safe_system, ensure_ascii=False), timestamp),
            )
            conn.execute(
                "INSERT INTO snapshots(session_id, through_seq, revision, context, updated_at)"
                " VALUES(?,?,?,?,?)",
                (
                    session_id,
                    1,
                    0,
                    json.dumps(context.to_payload(), ensure_ascii=False),
                    timestamp,
                ),
            )
        return info

    def latest(self, workspace: str | Path) -> SessionInfo | None:
        sessions = self.list_sessions(workspace)
        return sessions[0] if sessions else None

    def load(self, session_id: str, workspace: str | Path) -> SessionInfo:
        return self._fetch_info(session_id, self._workspace_root(workspace))

    def list_sessions(self, workspace: str | Path) -> list[SessionInfo]:
        root = self._workspace_root(workspace)
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE workspace_hash=? ORDER BY updated_at DESC, session_id",
            (self._workspace_hash(root),),
        ).fetchall()
        sessions: list[SessionInfo] = []
        for row in rows:
            try:
                info = self._row_to_info(row)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if Path(info.workspace) == root:
                sessions.append(info)
        return sessions

    def update_state(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        title: str | None = None,
        approved_tools: list[str] | None = None,
    ) -> SessionInfo:
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError("title 必须是非空字符串")
        if approved_tools is not None and (
            not isinstance(approved_tools, list)
            or not all(isinstance(item, str) and item for item in approved_tools)
        ):
            raise ValueError("approved_tools 必须是非空字符串组成的数组")
        root = self._workspace_root(workspace)
        with self._write_txn() as conn:
            info = self._fetch_info(session_id, root)
            new_title = title.strip() if title is not None else info.title
            new_tools = (
                list(dict.fromkeys(approved_tools))
                if approved_tools is not None
                else list(info.approved_tools)
            )
            updated_at = _now()
            conn.execute(
                "UPDATE sessions SET title=?, approved_tools=?, updated_at=? WHERE session_id=?",
                (new_title, json.dumps(new_tools, ensure_ascii=False), updated_at, session_id),
            )
        return SessionInfo(
            session_id=info.session_id,
            workspace=info.workspace,
            title=new_title,
            created_at=info.created_at,
            updated_at=updated_at,
            provider=info.provider,
            model=info.model,
            base_url=info.base_url,
            approved_tools=new_tools,
        )

    def update_runtime(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        provider: str,
        model: str | None,
        base_url: str | None,
    ) -> SessionInfo:
        SessionStore._validate_runtime_metadata(provider, model, base_url)
        root = self._workspace_root(workspace)
        with self._write_txn() as conn:
            info = self._fetch_info(session_id, root)
            updated_at = _now()
            conn.execute(
                "UPDATE sessions SET provider=?, model=?, base_url=?, updated_at=?"
                " WHERE session_id=?",
                (provider, model, base_url, updated_at, session_id),
            )
        return SessionInfo(
            session_id=info.session_id,
            workspace=info.workspace,
            title=info.title,
            created_at=info.created_at,
            updated_at=updated_at,
            provider=provider,
            model=model,
            base_url=base_url,
            approved_tools=list(info.approved_tools),
        )

    # ---- 日志与活动快照 ----

    def _last_seq(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE session_id=?", (session_id,)
        ).fetchone()
        return int(row["s"])

    def _read_snapshot(
        self, conn: sqlite3.Connection, session_id: str
    ) -> tuple[ActiveContext, int]:
        """读取并严格解析快照；未知版本/畸形显式失败（fail-closed）。"""
        row = conn.execute("SELECT * FROM snapshots WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return ActiveContext.empty(), 0
        try:
            value = json.loads(row["context"])
        except json.JSONDecodeError as exc:
            raise ValueError("会话活动上下文不是有效 JSON") from exc
        try:
            context = ActiveContext.from_payload(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return context, row["revision"]

    def _write_snapshot(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        context: ActiveContext,
        *,
        updated_at: str,
    ) -> None:
        redacted = ActiveContext(
            messages=redact(context.messages),
            through_seq=context.through_seq,
            snapshot_revision=context.snapshot_revision,
            pins=tuple(
                Pin(pin_id=p.pin_id, text=redact(p.text), seq=p.seq, created_at=p.created_at)
                for p in context.pins
            ),
            anchor=(
                None
                if context.anchor is None
                else TaskAnchor.build(
                    text=redact(context.anchor.text),
                    covered_through_seq=context.anchor.covered_through_seq,
                    sources=context.anchor.sources,
                    updated_at=context.anchor.updated_at,
                )
            ),
            goal=redact(context.goal),
        )
        encoded = json.dumps(redacted.to_payload(), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > self.max_bytes:
            raise ValueError(f"活动上下文超过写入字节上限 {self.max_bytes}")
        conn.execute(
            "INSERT INTO snapshots(session_id, through_seq, revision, context, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            " through_seq=excluded.through_seq, revision=excluded.revision,"
            " context=excluded.context, updated_at=excluded.updated_at",
            (session_id, context.through_seq, context.snapshot_revision, encoded, updated_at),
        )

    def _touch(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (_now(), session_id))

    @staticmethod
    def _check_expected_seq(expected_seq: int | None, current_seq: int) -> None:
        if expected_seq is not None and expected_seq != current_seq:
            raise RuntimeError("会话已被另一个进程更新；为避免覆盖新消息，请重新加载 session")

    @staticmethod
    def _check_expected_revision(expected_revision: int | None, current_revision: int) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数或 None")
        if expected_revision != current_revision:
            raise RuntimeError("会话快照已被另一个进程更新；为避免覆盖，请重新加载 session")

    def append_message(
        self,
        session_id: str,
        workspace: str | Path,
        message: dict[str, Any],
        *,
        active_messages: list[dict[str, Any]] | None = None,
        expected_seq: int | None = None,
    ) -> int:
        if not isinstance(message, dict):
            raise TypeError("message 必须是对象")
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        safe_message = redact(message)
        with self._write_txn() as conn:
            # 先在事务内校验快照可解析，避免写入新事件后才发现快照损坏。
            existing, _revision = self._read_snapshot(conn, session_id)
            current_seq = self._last_seq(conn, session_id)
            self._check_expected_seq(expected_seq, current_seq)
            sequence = current_seq + 1
            encoded = json.dumps(safe_message, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > self.max_bytes:
                raise ValueError(f"单条会话消息超过写入字节上限 {self.max_bytes}")
            conn.execute(
                "INSERT INTO events(session_id, seq, message, created_at) VALUES(?,?,?,?)",
                (session_id, sequence, encoded, _now()),
            )
            if active_messages is not None:
                updated = existing.with_messages(list(active_messages), sequence)
            else:
                # 未提供活动窗口时，必须把 through_seq 之后的 journal 尾部并入，
                # 否则水位线前移会把尾部消息跳过（读回时丢失）。
                merged = list(existing.messages)
                tail = conn.execute(
                    "SELECT message FROM events WHERE session_id=? AND seq>? ORDER BY seq",
                    (session_id, existing.through_seq),
                ).fetchall()
                merged.extend(json.loads(row["message"]) for row in tail)
                updated = existing.with_messages(merged, sequence)
            if len(updated.messages) > self.max_events:
                raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
            self._write_snapshot(conn, session_id, updated, updated_at=_now())
            self._touch(conn, session_id)
        return sequence

    def load_messages(self, session_id: str, workspace: str | Path) -> list[dict[str, Any]]:
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        rows = self._conn.execute(
            "SELECT seq, message FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        if len(rows) > self.max_events:
            raise ValueError(f"会话上下文超过消息上限 {self.max_events}")
        messages = [json.loads(row["message"]) for row in rows]
        if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            raise ValueError(f"会话上下文超过读取字节上限 {self.max_bytes}")
        return messages

    def _load_context(
        self, session_id: str, *, repair_incomplete: bool
    ) -> tuple[ActiveContext, int]:
        context, _revision = self._read_snapshot(self._conn, session_id)
        rows = self._conn.execute(
            "SELECT message FROM events WHERE session_id=? AND seq>? ORDER BY seq",
            (session_id, context.through_seq),
        ).fetchall()
        messages = list(context.messages)
        for row in rows:
            messages.append(json.loads(row["message"]))
        if repair_incomplete:
            messages = CheckpointMixin._repair_incomplete_tool_calls(messages)
        if len(messages) > self.max_events:
            raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
        if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            raise ValueError(f"会话活动上下文超过读取字节上限 {self.max_bytes}")
        last_seq = self._last_seq(self._conn, session_id)
        recovered = ActiveContext(
            messages=messages,
            through_seq=last_seq,
            snapshot_revision=context.snapshot_revision,
            pins=context.pins,
            anchor=context.anchor,
            goal=context.goal,
        )
        return recovered, last_seq

    def load_active_messages(
        self, session_id: str, workspace: str | Path, *, repair_incomplete: bool = False
    ) -> list[dict[str, Any]]:
        messages, _seq = self.load_active_snapshot(
            session_id, workspace, repair_incomplete=repair_incomplete
        )
        return messages

    def load_active_snapshot(
        self, session_id: str, workspace: str | Path, *, repair_incomplete: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        with self._lock:
            context, sequence = self._load_context(session_id, repair_incomplete=repair_incomplete)
        return context.messages, sequence

    def load_active_context(
        self, session_id: str, workspace: str | Path, *, repair_incomplete: bool = False
    ) -> ActiveContext:
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        with self._lock:
            context, _seq = self._load_context(session_id, repair_incomplete=repair_incomplete)
        return context

    def save_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        messages: list[dict[str, Any]],
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise TypeError("messages 必须是消息对象数组")
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        with self._write_txn() as conn:
            existing, revision = self._read_snapshot(conn, session_id)
            current_seq = self._last_seq(conn, session_id)
            self._check_expected_seq(expected_seq, current_seq)
            self._check_expected_revision(expected_revision, revision)
            updated = existing.with_messages(list(messages), current_seq)
            if expected_revision is not None and updated.snapshot_revision <= revision:
                raise ValueError("保存活动窗口时必须推进 snapshot_revision（内容需变化）")
            if len(updated.messages) > self.max_events:
                raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
            self._write_snapshot(conn, session_id, updated, updated_at=_now())
            self._touch(conn, session_id)

    def save_active_context(
        self,
        session_id: str,
        workspace: str | Path,
        context: ActiveContext,
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        if not isinstance(context, ActiveContext):
            raise TypeError("context 必须是 ActiveContext")
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        with self._write_txn() as conn:
            existing, revision = self._read_snapshot(conn, session_id)
            current_seq = self._last_seq(conn, session_id)
            self._check_expected_seq(expected_seq, current_seq)
            self._check_expected_revision(expected_revision, revision)
            if context.through_seq > current_seq:
                raise ValueError("会话活动上下文领先于 journal，拒绝写入")
            if len(context.messages) > self.max_events:
                raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
            if expected_revision is not None and context.snapshot_revision <= revision:
                raise ValueError("保存完整上下文时必须推进 snapshot_revision")
            self._write_snapshot(conn, session_id, context, updated_at=_now())
            self._touch(conn, session_id)

    # ---- 用量账本 ----

    def record_usage(
        self, session_id: str, workspace: str | Path, run_id: str, usage: dict[str, Any]
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id 必须是非空字符串")
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in _USAGE_FIELDS):
            raise ValueError("无效用量计数")
        root = self._workspace_root(workspace)
        with self._write_txn() as conn:
            self._fetch_info(session_id, root)
            exists = conn.execute(
                "SELECT 1 FROM usage_ledger WHERE session_id=? AND run_id=?",
                (session_id, run_id),
            ).fetchone()
            if exists is not None:
                return
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_ledger WHERE session_id=?", (session_id,)
            ).fetchone()["c"]
            if count >= self.max_events:
                raise ValueError("会话用量账本超过上限")
            conn.execute(
                "INSERT INTO usage_ledger(session_id, run_id, prompt_tokens, completion_tokens,"
                " total_tokens, requests, requests_with_usage, requests_without_usage, retries,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, run_id, *(usage[k] for k in _USAGE_FIELDS), _now()),
            )

    @staticmethod
    def _sum_usage(rows: list[sqlite3.Row]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in rows:
            values = {k: row[k] for k in _USAGE_FIELDS}
            if values["total_tokens"] < values["prompt_tokens"] + values["completion_tokens"]:
                raise ValueError("会话用量账本计数不一致")
            if (
                values["requests"]
                != values["requests_with_usage"] + values["requests_without_usage"]
            ):
                raise ValueError("会话用量账本计数不一致")
            for key, value in values.items():
                totals[key] = totals.get(key, 0) + value
        return totals

    def total_usage(self, session_id: str, workspace: str | Path) -> dict[str, int]:
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        rows = self._conn.execute(
            "SELECT * FROM usage_ledger WHERE session_id=?", (session_id,)
        ).fetchall()
        return self._sum_usage(rows)

    def workspace_usage(self, workspace: str | Path) -> dict[str, Any]:
        root = self._workspace_root(workspace)
        rows = self._conn.execute(
            "SELECT session_id FROM sessions WHERE workspace_hash=?",
            (self._workspace_hash(root),),
        ).fetchall()
        report: dict[str, Any] = {
            "totals": {},
            "session_count": 0,
            "recorded_sessions": 0,
            "unrecorded_sessions": 0,
            "unreadable_sessions": 0,
        }
        for row in rows:
            session_id = row["session_id"]
            try:
                info = self._fetch_info(session_id, root)
            except (ValueError, TypeError, KeyError):
                report["unreadable_sessions"] += 1
                continue
            if Path(info.workspace) != root:
                continue
            report["session_count"] += 1
            totals = self.total_usage(session_id, root)
            if not totals:
                report["unrecorded_sessions"] += 1
                continue
            report["recorded_sessions"] += 1
            for key, value in totals.items():
                report["totals"][key] = report["totals"].get(key, 0) + value
        return report

    def close(self) -> None:
        """关闭连接；调用方也可依赖进程退出回收。"""
        self._conn.close()

    def usage_records(self, session_id: str, workspace: str | Path) -> dict[str, dict[str, int]]:
        """返回按 run_id 分组的原始账本；用于迁移时保留幂等键。"""
        root = self._workspace_root(workspace)
        self._fetch_info(session_id, root)
        rows = self._conn.execute(
            "SELECT * FROM usage_ledger WHERE session_id=?", (session_id,)
        ).fetchall()
        records: dict[str, dict[str, int]] = {}
        for row in rows:
            records[row["run_id"]] = {key: int(row[key]) for key in _USAGE_FIELDS}
        self._sum_usage(rows)  # 顺带校验计数一致
        return records

    def import_session(
        self,
        info: SessionInfo,
        messages: list[dict[str, Any]],
        context: ActiveContext,
        usage: dict[str, dict[str, int]],
    ) -> bool:
        """单个事务导入一个会话；同名冲突显式失败，重复导入幂等。"""
        if not isinstance(info, SessionInfo):
            raise TypeError("info 必须是 SessionInfo")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise TypeError("messages 必须是消息对象数组")
        if not isinstance(context, ActiveContext):
            raise TypeError("context 必须是 ActiveContext")
        if len(messages) > self.max_events:
            raise ValueError(f"会话消息超过上限 {self.max_events}")
        SessionStore._validate_runtime_metadata(info.provider, info.model, info.base_url)
        root = self._workspace_root(info.workspace)
        if Path(info.workspace) != root:
            raise ValueError("会话所属工作区与请求工作区不一致")
        self._validate_session_id(info.session_id)
        for record in usage.values():
            if any(type(record.get(k)) is not int or record[k] < 0 for k in _USAGE_FIELDS):
                raise ValueError("无效用量计数")

        with self._write_txn() as conn:
            existing = conn.execute(
                "SELECT created_at FROM sessions WHERE session_id=?", (info.session_id,)
            ).fetchone()
            if existing is not None:
                if existing["created_at"] != info.created_at:
                    raise ValueError("目标已存在同名但不同的会话，拒绝覆盖")
                return False
            conn.execute(
                "INSERT INTO sessions(session_id, workspace, workspace_hash, title, provider,"
                " model, base_url, approved_tools, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    info.session_id,
                    info.workspace,
                    self._workspace_hash(root),
                    info.title,
                    info.provider,
                    info.model,
                    info.base_url,
                    json.dumps(list(info.approved_tools), ensure_ascii=False),
                    info.created_at,
                    info.updated_at,
                ),
            )
            for sequence, message in enumerate(messages, start=1):
                conn.execute(
                    "INSERT INTO events(session_id, seq, message, created_at) VALUES(?,?,?,?)",
                    (
                        info.session_id,
                        sequence,
                        json.dumps(redact(message), ensure_ascii=False),
                        info.updated_at,
                    ),
                )
            self._write_snapshot(conn, info.session_id, context, updated_at=info.updated_at)
            for run_id, record in usage.items():
                conn.execute(
                    "INSERT INTO usage_ledger(session_id, run_id, prompt_tokens,"
                    " completion_tokens, total_tokens, requests, requests_with_usage,"
                    " requests_without_usage, retries, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        info.session_id,
                        run_id,
                        *(record[k] for k in _USAGE_FIELDS),
                        info.updated_at,
                    ),
                )
        return True
