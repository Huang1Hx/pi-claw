"""按工作区隔离的本地会话持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .trace import redact
from .workspace import Workspace

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


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
    ) -> None:
        if max_events <= 0 or max_bytes <= 0:
            raise ValueError("会话 max_events 与 max_bytes 必须大于 0")
        configured_home = os.environ.get("PI_CLAW_HOME")
        default_home = (
            Path(configured_home).expanduser() if configured_home else Path.home() / ".pi-claw"
        )
        selected = Path(data_dir) if data_dir is not None else default_home / "sessions"
        self.data_dir = selected.expanduser().resolve(strict=False)
        self.max_events = max_events
        self.max_bytes = max_bytes

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
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider 必须是非空字符串")
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
        return info

    def append_message(
        self,
        session_id: str,
        workspace: str | Path,
        message: dict[str, Any],
    ) -> None:
        if not isinstance(message, dict):
            raise TypeError("message 必须是对象")
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        info = self.load(session_id, root)
        safe_message = redact(message)
        encoded = json.dumps(safe_message, ensure_ascii=False, separators=(",", ":"))
        context_path = session_dir / "context.jsonl"
        with context_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            stream.flush()
        context_path.chmod(0o600)
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

    def load_messages(
        self,
        session_id: str,
        workspace: str | Path,
    ) -> list[dict[str, Any]]:
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        context_path = session_dir / "context.jsonl"
        messages: list[dict[str, Any]] = []
        if not context_path.exists():
            return messages
        total_bytes = 0
        line_number = 0
        with context_path.open("rb") as stream:
            while True:
                remaining = self.max_bytes - total_bytes
                raw_line = stream.readline(remaining + 1)
                if not raw_line:
                    break
                line_number += 1
                total_bytes += len(raw_line)
                if total_bytes > self.max_bytes:
                    raise ValueError(f"会话上下文超过读取字节上限 {self.max_bytes}")
                if not raw_line.strip():
                    continue
                if len(messages) >= self.max_events:
                    raise ValueError(f"会话上下文超过消息上限 {self.max_events}")
                try:
                    line = raw_line.decode("utf-8")
                    value = json.loads(line)
                except UnicodeDecodeError as exc:
                    raise ValueError(f"会话上下文第 {line_number} 行不是有效 UTF-8") from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(f"会话上下文第 {line_number} 行不是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"会话上下文第 {line_number} 行必须是对象")
                messages.append(value)
        return messages

    def load_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
    ) -> list[dict[str, Any]]:
        """加载上次持久化的活动窗口；不存在时退回完整 append-only 历史。"""

        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        active_path = session_dir / "active_context.json"
        if not active_path.exists():
            return self.load_messages(session_id, root)
        try:
            raw = active_path.read_bytes()
        except OSError as exc:
            raise ValueError("无法读取会话活动上下文") from exc
        if len(raw) > self.max_bytes:
            raise ValueError(f"会话活动上下文超过读取字节上限 {self.max_bytes}")
        try:
            value = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("会话活动上下文不是有效 UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("会话活动上下文不是有效 JSON") from exc
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError("会话活动上下文必须是消息对象数组")
        if len(value) > self.max_events:
            raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
        return value

    def save_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        messages: list[dict[str, Any]],
    ) -> None:
        """原子保存压缩后的活动窗口；不修改 context.jsonl 原始历史。"""

        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise TypeError("messages 必须是消息对象数组")
        if len(messages) > self.max_events:
            raise ValueError(f"活动上下文超过消息上限 {self.max_events}")
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        encoded = json.dumps(
            redact(messages),
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
        self._write_state(self._session_dir(session_id, root), updated)
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

        if not isinstance(provider, str) or not provider:
            raise ValueError("provider 必须是非空字符串")
        if model is not None and not isinstance(model, str):
            raise TypeError("model 必须是字符串或 None")
        if base_url is not None and not isinstance(base_url, str):
            raise TypeError("base_url 必须是字符串或 None")
        root = self._workspace_root(workspace)
        info = self.load(session_id, root)
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
        self._write_state(self._session_dir(session_id, root), updated)
        return updated

    @staticmethod
    def _workspace_root(workspace: str | Path) -> Path:
        return Workspace(workspace).root

    def _workspace_group(self, root: Path) -> Path:
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
        return self.data_dir / digest

    def _session_dir(self, session_id: str, root: Path) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session_id 格式不安全")
        return self._workspace_group(root) / session_id

    def _secure_session_tree(self, root: Path, session_dir: Path) -> None:
        """会话可能含私有代码片段；显式限制为当前本机用户可读写。"""

        for directory in (self.data_dir, self._workspace_group(root), session_dir):
            directory.chmod(0o700)

    @staticmethod
    def _read_state(session_dir: Path) -> SessionInfo:
        state_path = session_dir / "state.json"
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
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise
