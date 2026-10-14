"""追加式原始消息日志、分段和尾部恢复。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from persistence.models import _SEGMENT_PATTERN, SessionInfo, _now
from persistence.trace import redact


class JournalMixin:
    """SessionStore 内部协作组件，共享会话路径和读取预算。"""

    def append_message(
        self,
        session_id: str,
        workspace: str | Path,
        message: dict[str, Any],
        *,
        active_messages: list[dict[str, Any]] | None = None,
        expected_seq: int | None = None,
    ) -> int:
        """在会话锁内检查 expected_seq，追加原始消息并推进 checkpoint。

        expected_seq 是调用方加载快照时观察到的版本，冲突则拒绝提交，不能静默覆盖。
        active_messages 可提供已压缩的活动窗口；返回成功追加后的日志序号。
        原始消息先脱敏、追加并 fsync，再更新元数据和快照；不是跨文件原子事务。
        """
        if not isinstance(message, dict):
            raise TypeError("message 必须是对象")
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            info = self._read_state(session_dir)
            # 版本/格式预检必须先于任何 journal/state/snapshot 修改；未知或畸形快照直接失败。
            self._precheck_snapshot_unlocked(session_dir)
            current_seq = self._journal_last_seq(session_dir)
            # 排他锁保护这次读改写；序号再防止“拿旧上下文、等到锁后覆盖新消息”。
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
            # 若后续 state/checkpoint 保存失败，恢复流程仍可从先落盘的 journal 补齐尾部。
            self._write_state(session_dir, updated)
            active_path = session_dir / "active_context.json"
            if active_messages is not None:
                self._save_active_messages_unlocked(session_dir, active_messages, sequence)
            elif active_path.exists():
                # 吸收本次新消息序号，同时保留快照里已有的 pins/anchor。
                active_context, _loaded_seq = self._load_active_context_unlocked(session_dir)
                self._save_active_messages_unlocked(session_dir, active_context.messages, sequence)
            return sequence

    def load_messages(
        self,
        session_id: str,
        workspace: str | Path,
    ) -> list[dict[str, Any]]:
        """在锁内读取完整原始消息日志。"""
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            return self._load_all_messages_unlocked(session_dir)

    def _load_all_messages_unlocked(self, session_dir: Path) -> list[dict[str, Any]]:
        """由持锁调用方按段读取完整日志，检查字节/消息预算和严格递增序号。"""
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

    def _journal_paths(self, session_dir: Path) -> list[Path]:
        """按顺序返回旧 context.jsonl 与编号分段，拒绝日志文件符号链接。"""
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
        """选择可追加的最后一段；遇到半写尾行或容量上限时新开分段，不覆盖旧日志。"""
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
        """恢复一条日志的序号和消息；兼容无序号旧格式，并拒绝倒退的显式序号。"""
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
        """从最后一段推导最新耐久序号；半写且未换行的尾记录不算已提交。"""
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
        """按剩余读取预算加载 through_seq 之后的增量消息，跳过已被快照吸收的旧分段。"""
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
