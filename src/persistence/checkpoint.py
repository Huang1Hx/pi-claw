"""活动上下文 checkpoint 与未完成工具协议修复。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from persistence.trace import redact


class CheckpointMixin:
    """SessionStore 内部协作组件，共享会话路径和读取预算。"""

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
        """在同一把会话锁内返回 (活动消息, 最新 journal 序号)。

        返回序号应作为下一次保存的 expected_seq，避免基于旧快照提交。
        repair_incomplete 只补齐中断调用的失败消息，不重跑工具、不宣称操作未曾发生。
        """

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
        """在调用方已持锁时读取 checkpoint 并补齐 journal 尾部，返回活动消息和最新序号。"""
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
        # checkpoint 可以落后于 journal：仅重读 through_seq 之后的消息，避免丢尾或重复。
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
        """在调用方已持锁时写入脱敏的 v2 快照，through_seq 表示已吸收的日志位置。"""
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

    @staticmethod
    def _repair_incomplete_tool_calls(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """为缺失结果的调用补充显式失败消息以恢复协议配对；绝不重放工具或伪造成功。"""
        repaired: list[dict[str, Any]] = []
        pending: dict[str, str] = {}

        def flush_pending() -> None:
            """把尚未配对的调用标为 synthetic_recovery 失败，并清空待处理集合。"""
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
