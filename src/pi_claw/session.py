"""按工作区隔离的本地会话持久化。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .trace import redact
from .workspace import Workspace

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SEGMENT_PATTERN = re.compile(r"^context-(\d{20})\.jsonl$")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    workspace: str
    title: str
    created_at: str
    updated_at: str
    provider: str = "scripted"
    model: str | None = None
    base_url: str | None = None
    approved_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "approved_tools": list(self.approved_tools),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionInfo:
        approved = value.get("approved_tools", [])
        if not isinstance(approved, list) or not all(isinstance(item, str) for item in approved):
            raise ValueError("会话 approved_tools 必须是字符串数组")
        return cls(
            session_id=str(value["session_id"]),
            workspace=str(value["workspace"]),
            title=str(value["title"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            provider=str(value.get("provider", "scripted")),
            model=str(value["model"]) if value.get("model") is not None else None,
            base_url=(str(value["base_url"]) if value.get("base_url") is not None else None),
            approved_tools=list(approved),
        )


class SessionStore:
    """保存 context.jsonl 与 state.json，且禁止跨工作区加载会话。"""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        max_events: int = 10_000,
        max_bytes: int = 20_000_000,
        max_segment_bytes: int = 5_000_000,
    ) -> None:
        if max_events <= 0 or max_bytes <= 0 or max_segment_bytes <= 0:
            raise ValueError("会话事件、总字节与分段字节上限必须大于 0")
        configured_home = os.environ.get("PI_CLAW_HOME")
        default_home = (
            Path(configured_home).expanduser() if configured_home else Path.home() / ".pi-claw"
        )
        selected = Path(data_dir) if data_dir is not None else default_home / "sessions"
        self.data_dir = selected.expanduser().resolve(strict=False)
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.max_segment_bytes = max_segment_bytes

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
        self._validate_runtime_metadata(provider, model, base_url)
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
        session_dir = self._session_dir(session_id, root)
        session_dir.mkdir(parents=True, exist_ok=False)
        self._secure_session_tree(root, session_dir)
        self._write_state(session_dir, info)
        self.append_message(
            session_id,
            root,
            {"role": "system", "content": system_prompt},
        )
        self.save_active_messages(
            session_id,
            root,
            [{"role": "system", "content": system_prompt}],
        )
        return self.load(session_id, root)

    def latest(self, workspace: str | Path) -> SessionInfo | None:
        sessions = self.list_sessions(workspace)
        return sessions[0] if sessions else None

    def load(self, session_id: str, workspace: str | Path) -> SessionInfo:
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        info = self._read_state(session_dir)
        if Path(info.workspace) != root:
            raise ValueError("会话所属工作区与请求工作区不一致")
        if info.session_id != session_id:
            raise ValueError("会话状态中的 session_id 不一致")
        self._secure_session_tree(root, session_dir)
        self._secure_session_files(session_dir)
        return info

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
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            info = self._read_state(session_dir)
            current_seq = self._journal_last_seq(session_dir)
            self._check_expected_seq(expected_seq, current_seq)
            safe_message = redact(message)
            sequence = current_seq + 1
            row = {"seq": sequence, "message": safe_message}
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            encoded_size = len(encoded.encode("utf-8")) + 1
            if encoded_size > self.max_segment_bytes:
                raise ValueError(f"单条会话消息超过分段字节上限 {self.max_segment_bytes}")
            context_path = self._journal_append_path(session_dir, sequence, encoded_size)
            self._reject_symlink(context_path)
            with context_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            context_path.chmod(0o600)
            self._fsync_directory(session_dir)
            # 每条新消息都会推进 updated_at；state 使用原子替换，避免半写文件。
            updated = SessionInfo(
                session_id=info.session_id,
                workspace=info.workspace,
                title=info.title,
                created_at=info.created_at,
                updated_at=_now(),
                provider=info.provider,
                model=info.model,
                base_url=info.base_url,
                approved_tools=list(info.approved_tools),
            )
            self._write_state(session_dir, updated)
            active_path = session_dir / "active_context.json"
            if active_messages is not None:
                self._save_active_messages_unlocked(session_dir, active_messages, sequence)
            elif active_path.exists():
                active, _loaded_seq = self._load_active_messages_unlocked(session_dir)
                self._save_active_messages_unlocked(session_dir, active, sequence)
            return sequence

    def load_messages(
        self,
        session_id: str,
        workspace: str | Path,
    ) -> list[dict[str, Any]]:
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            return self._load_all_messages_unlocked(session_dir)

    def _load_all_messages_unlocked(self, session_dir: Path) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        total_bytes = 0
        sequence = 0
        for path in self._journal_paths(session_dir):
            for row, raw_size in self._read_journal_segment(path):
                total_bytes += raw_size
                if total_bytes > self.max_bytes:
                    raise ValueError(f"会话上下文超过读取字节上限 {self.max_bytes}")
                if len(messages) >= self.max_events:
                    raise ValueError(f"会话上下文超过消息上限 {self.max_events}")
                sequence, message = self._decode_journal_row(row, sequence)
                messages.append(message)
        return messages

    def load_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        """加载上次持久化的活动窗口；不存在时退回完整 append-only 历史。"""

        messages, _sequence = self.load_active_snapshot(
            session_id,
            workspace,
            repair_incomplete=repair_incomplete,
        )
        return messages

    def load_active_snapshot(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """一致地返回活动窗口和对应的 journal 序号。"""

        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            return self._load_active_messages_unlocked(
                session_dir,
                repair_incomplete=repair_incomplete,
            )

    def _load_active_messages_unlocked(
        self,
        session_dir: Path,
        *,
        repair_incomplete: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        active_path = session_dir / "active_context.json"
        if not active_path.exists():
            messages: list[dict[str, Any]] = []
            through_seq = 0
            checkpoint_bytes = 0
        else:
            self._reject_symlink(active_path)
            try:
                raw = active_path.read_bytes()
            except OSError as exc:
                raise ValueError("无法读取会话活动上下文") from exc
            if len(raw) > self.max_bytes:
                raise ValueError(f"会话活动上下文超过读取字节上限 {self.max_bytes}")
            checkpoint_bytes = len(raw)
            try:
                value = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("会话活动上下文不是有效 UTF-8") from exc
            except json.JSONDecodeError as exc:
                raise ValueError("会话活动上下文不是有效 JSON") from exc
            if isinstance(value, list):
                # v1 旧格式没有 checkpoint 序号，无法证明它吸收了 journal 的哪一段。
                # 迁移时以 durable journal 为准，宁可暂时失去压缩也不能丢尾消息。
                messages = self._load_all_messages_unlocked(session_dir)
                through_seq = self._journal_last_seq(session_dir)
                checkpoint_bytes = 0
            elif isinstance(value, dict):
                if value.get("version") != 2:
                    raise ValueError("会话活动上下文版本不受支持")
                messages = value.get("messages")
                through_seq = value.get("through_seq")
            else:
                raise ValueError("会话活动上下文格式无效")
            if not isinstance(messages, list) or not all(
                isinstance(item, dict) for item in messages
            ):
                raise ValueError("会话活动上下文必须包含消息对象数组")
            if not isinstance(through_seq, int) or through_seq < 0:
                raise ValueError("会话活动上下文 through_seq 无效")
            if through_seq > self._journal_last_seq(session_dir):
                raise ValueError("会话活动上下文领先于 journal，拒绝跳过历史")
        tail = self._load_journal_after(
            session_dir,
            through_seq,
            max_bytes=self.max_bytes - checkpoint_bytes,
        )
        messages.extend(tail)
        if repair_incomplete:
            messages = self._repair_incomplete_tool_calls(messages)
        if len(messages) > self.max_events:
            raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
        return messages, self._journal_last_seq(session_dir)

    def save_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        messages: list[dict[str, Any]],
        *,
        expected_seq: int | None = None,
    ) -> None:
        """原子保存压缩后的活动窗口；不修改 context.jsonl 原始历史。"""

        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise TypeError("messages 必须是消息对象数组")
        if len(messages) > self.max_events:
            raise ValueError(f"活动上下文超过消息上限 {self.max_events}")
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            current_seq = self._journal_last_seq(session_dir)
            self._check_expected_seq(expected_seq, current_seq)
            self._save_active_messages_unlocked(session_dir, messages, current_seq)

    def _save_active_messages_unlocked(
        self,
        session_dir: Path,
        messages: list[dict[str, Any]],
        through_seq: int,
    ) -> None:
        checkpoint = {
            "version": 2,
            "through_seq": through_seq,
            "messages": redact(messages),
        }
        encoded = json.dumps(
            checkpoint,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError(f"活动上下文超过写入字节上限 {self.max_bytes}")
        self._atomic_write(session_dir / "active_context.json", encoded + b"\n")

    def list_sessions(self, workspace: str | Path) -> list[SessionInfo]:
        root = self._workspace_root(workspace)
        group = self._workspace_group(root)
        if not group.exists():
            return []
        sessions: list[SessionInfo] = []
        for child in group.iterdir():
            if not child.is_dir() or not _SESSION_ID_PATTERN.fullmatch(child.name):
                continue
            try:
                info = self.load(child.name, root)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            sessions.append(info)
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def update_state(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        title: str | None = None,
        approved_tools: list[str] | None = None,
    ) -> SessionInfo:
        root = self._workspace_root(workspace)
        info = self.load(session_id, root)
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError("title 必须是非空字符串")
        if approved_tools is not None and (
            not isinstance(approved_tools, list)
            or not all(isinstance(item, str) and item for item in approved_tools)
        ):
            raise ValueError("approved_tools 必须是非空字符串组成的数组")
        session_dir = self._session_dir(session_id, root)
        with self._session_write_lock(session_dir):
            info = self._read_state(session_dir)
            updated = SessionInfo(
                session_id=info.session_id,
                workspace=info.workspace,
                title=title.strip() if title is not None else info.title,
                created_at=info.created_at,
                updated_at=_now(),
                provider=info.provider,
                model=info.model,
                base_url=info.base_url,
                approved_tools=(
                    list(dict.fromkeys(approved_tools))
                    if approved_tools is not None
                    else list(info.approved_tools)
                ),
            )
            self._write_state(session_dir, updated)
            return updated

    def update_runtime(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        provider: str,
        model: str | None,
        base_url: str | None,
    ) -> SessionInfo:
        """保存可恢复的 Provider 标识；API key 永不进入会话状态。"""

        self._validate_runtime_metadata(provider, model, base_url)
        root = self._workspace_root(workspace)
        self.load(session_id, root)
        session_dir = self._session_dir(session_id, root)
        with self._session_write_lock(session_dir):
            info = self._read_state(session_dir)
            updated = SessionInfo(
                session_id=info.session_id,
                workspace=info.workspace,
                title=info.title,
                created_at=info.created_at,
                updated_at=_now(),
                provider=provider,
                model=model,
                base_url=base_url,
                approved_tools=list(info.approved_tools),
            )
            self._write_state(session_dir, updated)
            return updated

    @staticmethod
    def _workspace_root(workspace: str | Path) -> Path:
        return Workspace(workspace).root

    def _journal_paths(self, session_dir: Path) -> list[Path]:
        paths: list[Path] = []
        legacy = session_dir / "context.jsonl"
        if legacy.exists():
            self._reject_symlink(legacy)
            paths.append(legacy)
        segments = sorted(
            path
            for path in session_dir.iterdir()
            if path.is_file() and _SEGMENT_PATTERN.fullmatch(path.name)
        )
        for path in segments:
            self._reject_symlink(path)
        paths.extend(segments)
        return paths

    def _journal_append_path(self, session_dir: Path, sequence: int, row_bytes: int) -> Path:
        paths = self._journal_paths(session_dir)
        if not paths:
            return session_dir / "context.jsonl"
        current = paths[-1]
        size = current.stat().st_size
        has_partial_tail = False
        if size:
            with current.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                has_partial_tail = stream.read(1) != b"\n"
        if has_partial_tail or (size and size + row_bytes > self.max_segment_bytes):
            return session_dir / f"context-{sequence:020d}.jsonl"
        return current

    def _read_journal_segment(self, path: Path):
        """最后一条半写 JSON 可忽略；带换行的损坏中间记录必须报错。"""

        self._reject_symlink(path)
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.endswith(b"\n"):
                    break
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise ValueError(f"会话段 {path.name} 第 {line_number} 行不是 UTF-8") from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(f"会话段 {path.name} 第 {line_number} 行不是 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"会话段 {path.name} 第 {line_number} 行必须是对象")
                yield value, len(raw_line)

    @staticmethod
    def _decode_journal_row(
        row: dict[str, Any],
        previous_seq: int,
    ) -> tuple[int, dict[str, Any]]:
        if "seq" not in row and "message" not in row:
            return previous_seq + 1, row
        sequence = row.get("seq")
        message = row.get("message")
        if not isinstance(sequence, int) or sequence <= previous_seq:
            raise ValueError("会话 journal seq 必须严格递增")
        if not isinstance(message, dict):
            raise ValueError("会话 journal message 必须是对象")
        return sequence, message

    def _journal_last_seq(self, session_dir: Path) -> int:
        paths = self._journal_paths(session_dir)
        if not paths:
            return 0
        path = paths[-1]
        match = _SEGMENT_PATTERN.fullmatch(path.name)
        sequence = int(match.group(1)) - 1 if match else 0
        for row, _raw_size in self._read_journal_segment(path):
            sequence, _message = self._decode_journal_row(row, sequence)
        return sequence

    def _load_journal_after(
        self,
        session_dir: Path,
        through_seq: int,
        *,
        max_bytes: int,
    ) -> list[dict[str, Any]]:
        if max_bytes < 0:
            raise ValueError(f"会话活动上下文超过读取字节上限 {self.max_bytes}")
        paths = self._journal_paths(session_dir)
        messages: list[dict[str, Any]] = []
        total_bytes = 0
        for index, path in enumerate(paths):
            next_start = None
            if index + 1 < len(paths):
                next_match = _SEGMENT_PATTERN.fullmatch(paths[index + 1].name)
                if next_match:
                    next_start = int(next_match.group(1))
            if next_start is not None and next_start <= through_seq:
                continue
            match = _SEGMENT_PATTERN.fullmatch(path.name)
            sequence = int(match.group(1)) - 1 if match else 0
            for row, raw_size in self._read_journal_segment(path):
                sequence, message = self._decode_journal_row(row, sequence)
                if sequence > through_seq:
                    total_bytes += raw_size
                    if total_bytes > max_bytes:
                        raise ValueError(
                            f"会话活动上下文与 journal 尾部超过读取字节上限 {self.max_bytes}"
                        )
                    messages.append(message)
                    if len(messages) > self.max_events:
                        raise ValueError(f"会话 journal 尾部超过消息上限 {self.max_events}")
        return messages

    @staticmethod
    def _repair_incomplete_tool_calls(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        repaired: list[dict[str, Any]] = []
        pending: dict[str, str] = {}

        def flush_pending() -> None:
            for call_id, name in pending.items():
                result = {
                    "call_id": call_id,
                    "tool_name": name,
                    "ok": False,
                    "output": None,
                    "error": "会话在工具结果落盘前中断；该调用未被重放",
                    "metadata": {"synthetic_recovery": True},
                }
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            pending.clear()

        for message in messages:
            role = message.get("role")
            calls = message.get("tool_calls") if role == "assistant" else None
            if calls:
                if pending:
                    flush_pending()
                if not isinstance(calls, list):
                    raise ValueError("assistant.tool_calls 必须是数组")
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                        raise ValueError("assistant.tool_calls 缺少有效 id")
                    function = call.get("function", {})
                    name = (
                        function.get("name", "unknown") if isinstance(function, dict) else "unknown"
                    )
                    pending[call["id"]] = str(name)
                repaired.append(message)
                continue
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError("会话 journal 包含孤立 tool 消息")
                pending.pop(call_id)
                repaired.append(message)
                continue
            if pending:
                flush_pending()
            repaired.append(message)
        if pending:
            flush_pending()
        return repaired

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _session_write_lock(self, session_dir: Path):
        """跨进程串行化 journal/state/checkpoint 的读改写提交。"""

        lock_path = session_dir / ".session.lock"
        self._reject_symlink(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _check_expected_seq(expected_seq: int | None, current_seq: int) -> None:
        if expected_seq is not None and expected_seq != current_seq:
            raise RuntimeError("会话已被另一个进程更新；为避免覆盖新消息，请重新加载 session")

    def _workspace_group(self, root: Path) -> Path:
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
        return self.data_dir / digest

    def _session_dir(self, session_id: str, root: Path) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session_id 格式不安全")
        group = self._workspace_group(root)
        if group.is_symlink():
            raise ValueError("会话工作区目录不得是符号链接")
        candidate = group / session_id
        if candidate.is_symlink():
            raise ValueError("会话目录不得是符号链接")
        return candidate

    def _secure_session_tree(self, root: Path, session_dir: Path) -> None:
        """会话可能含私有代码片段；显式限制为当前本机用户可读写。"""

        for directory in (self.data_dir, self._workspace_group(root), session_dir):
            directory.chmod(0o700)

    @classmethod
    def _secure_session_files(cls, session_dir: Path) -> None:
        fixed = [
            session_dir / name
            for name in ("state.json", "context.jsonl", "active_context.json", ".session.lock")
        ]
        segments = [
            path
            for path in session_dir.glob("context-*.jsonl")
            if _SEGMENT_PATTERN.fullmatch(path.name)
        ]
        for path in (*fixed, *segments):
            if not path.exists():
                continue
            cls._reject_symlink(path)
            path.chmod(0o600)

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"会话文件不得是符号链接：{path.name}")

    @staticmethod
    def _validate_runtime_metadata(
        provider: str,
        model: str | None,
        base_url: str | None,
    ) -> None:
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider 必须是非空字符串")
        if model is not None and not isinstance(model, str):
            raise TypeError("model 必须是字符串或 None")
        if base_url is None:
            return
        if not isinstance(base_url, str):
            raise TypeError("base_url 必须是字符串或 None")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url 必须是无凭据、查询参数和片段的 HTTP(S) API 根地址")

    @staticmethod
    def _read_state(session_dir: Path) -> SessionInfo:
        state_path = session_dir / "state.json"
        SessionStore._reject_symlink(state_path)
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("会话不存在或不属于当前工作区") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("会话 state.json 不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("会话 state.json 必须是对象")
        return SessionInfo.from_dict(value)

    @staticmethod
    def _write_state(session_dir: Path, info: SessionInfo) -> None:
        session_dir.mkdir(parents=True, exist_ok=True)
        safe_state = redact(info.to_dict())
        encoded = (
            json.dumps(safe_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        SessionStore._atomic_write(session_dir / "state.json", encoded)

    @staticmethod
    def _atomic_write(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            path.chmod(0o600)
            SessionStore._fsync_directory(path.parent)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise
