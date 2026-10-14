"""会话门面：组合日志、活动窗口、锁与原子存储。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.active_context import ActiveContext
from persistence.checkpoint import CheckpointMixin
from persistence.journal import JournalMixin
from persistence.locking import LockingMixin
from persistence.models import _SEGMENT_PATTERN, _SESSION_ID_PATTERN, SessionInfo, _now
from persistence.storage import StorageMixin
from persistence.trace import redact
from persistence.usage import UsageMixin
from security.workspace import Workspace


class SessionStore(JournalMixin, CheckpointMixin, LockingMixin, StorageMixin, UsageMixin):
    """按工作区管理会话；保持既有磁盘格式和 seq 并发协议。"""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        max_events: int = 10_000,
        max_bytes: int = 20_000_000,
        max_segment_bytes: int = 5_000_000,
    ) -> None:
        """选择会话根目录并设置读取/分段预算；显式 data_dir 优先于 PI_CLAW_HOME 默认路径。"""
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
        """创建工作区隔离的会话及初始系统消息。"""
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
        """选取当前工作区最近更新的会话。"""
        sessions = self.list_sessions(workspace)
        return sessions[0] if sessions else None

    def load(self, session_id: str, workspace: str | Path) -> SessionInfo:
        """验证会话身份与工作区归属，并恢复元数据。"""
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

    def list_sessions(self, workspace: str | Path) -> list[SessionInfo]:
        """列出当前工作区合法会话，按更新时间排序。"""
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
        """在锁内更新标题或审批 scope，避免并发覆盖。"""
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

    def import_session(
        self,
        info: SessionInfo,
        messages: list[dict[str, Any]],
        context: ActiveContext,
        usage: dict[str, dict[str, Any]],
    ) -> bool:
        """原子导入一个会话（M5 迁移专用）；同名冲突显式失败，重复导入幂等。

        先在临时目录写全 state/journal/snapshot/usage，再原子重命名到目标目录；
        因此中断只会留下不可见的临时目录，不会产生“半成品会话”。
        """
        if not isinstance(info, SessionInfo):
            raise TypeError("info 必须是 SessionInfo")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise TypeError("messages 必须是消息对象数组")
        if not isinstance(context, ActiveContext):
            raise TypeError("context 必须是 ActiveContext")
        if len(messages) > self.max_events:
            raise ValueError(f"会话消息超过上限 {self.max_events}")
        self._validate_runtime_metadata(info.provider, info.model, info.base_url)
        root = self._workspace_root(info.workspace)
        if Path(info.workspace) != root:
            raise ValueError("会话所属工作区与请求工作区不一致")
        if not _SESSION_ID_PATTERN.fullmatch(info.session_id):
            raise ValueError("session_id 格式不安全")
        self._sum_usage(usage)  # 迁移前校验账本，坏账本不写入

        group = self._workspace_group(root)
        session_dir = self._session_dir(info.session_id, root)
        if session_dir.exists():
            existing = self.load(info.session_id, root)
            if existing.created_at != info.created_at:
                raise ValueError("目标已存在同名但不同的会话，拒绝覆盖")
            return False

        group.mkdir(parents=True, exist_ok=True)
        group.chmod(0o700)
        staging = group / f".{info.session_id}.import"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            self._write_state(staging, info)
            journal_path = staging / "context.jsonl"
            with journal_path.open("w", encoding="utf-8") as stream:
                for sequence, message in enumerate(messages, start=1):
                    row = {"seq": sequence, "message": redact(message)}
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            journal_path.chmod(0o600)
            self._save_active_context_unlocked(staging, context)
            usage_path = staging / "usage.json"
            if usage:
                self._atomic_write(
                    usage_path, json.dumps(usage, ensure_ascii=False).encode("utf-8")
                )
            staging.chmod(0o700)
            os.rename(staging, session_dir)
            self._fsync_directory(group)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return True

    @staticmethod
    def _workspace_root(workspace: str | Path) -> Path:
        """用 Workspace 校验目标目录，并返回消除符号链接后的真实工作区路径。"""
        return Workspace(workspace).root

    def _workspace_group(self, root: Path) -> Path:
        """对工作区真实路径取摘要以分组会话；这是路径隔离，不是加密或访问授权。"""
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
        return self.data_dir / digest

    def _session_dir(self, session_id: str, root: Path) -> Path:
        """校验会话 ID 并拼接工作区分组路径，拒绝分组或会话目录是符号链接。"""
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
        """将已存在的固定文件和日志分段收紧为 0600；拒绝符号链接，跳过缺失文件。"""
        fixed = [
            session_dir / name
            for name in (
                "state.json",
                "context.jsonl",
                "active_context.json",
                "usage.json",
                ".session.lock",
            )
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
    def _validate_runtime_metadata(
        provider: str,
        model: str | None,
        base_url: str | None,
    ) -> None:
        """校验可持久化的 Provider 信息，拒绝在 API 地址中夹带凭据或查询参数。"""
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
        """读取并校验 state.json，转换为 SessionInfo；缺失或损坏以可读错误报告。"""
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
        """脱敏并序列化会话元数据，以原子替换写入 state.json；并发更新由外层加锁。"""
        session_dir.mkdir(parents=True, exist_ok=True)
        safe_state = redact(info.to_dict())
        encoded = (
            json.dumps(safe_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        SessionStore._atomic_write(session_dir / "state.json", encoded)
