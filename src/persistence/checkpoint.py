"""活动上下文 checkpoint 与未完成工具协议修复。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.active_context import ActiveContext, ActiveContextError, Pin, TaskAnchor
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
            context, sequence = self._load_active_context_unlocked(
                session_dir,
                repair_incomplete=repair_incomplete,
            )
            return context.messages, sequence

    def load_active_context(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> ActiveContext:
        """加载完整活动上下文（含 pins、任务锚、快照修订号），并补齐 journal 尾部。"""

        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            context, _sequence = self._load_active_context_unlocked(
                session_dir,
                repair_incomplete=repair_incomplete,
            )
            return context

    def _read_snapshot_unlocked(self, session_dir: Path) -> tuple[ActiveContext, int]:
        """持锁读取并解析快照，返回 (上下文, 快照字节数)。

        v1（list）没有水位线，无法证明吸收到哪一段，回退到完整 journal 以免丢尾；
        v2/v3 由 ActiveContext 解析，v2 自动升级为当前格式且 pins/anchor 为空。
        """

        active_path = session_dir / "active_context.json"
        if not active_path.exists():
            return ActiveContext.empty(), 0
        self._reject_symlink(active_path)
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
        if isinstance(value, list):
            # v1 旧格式：以 durable journal 为准，宁可暂时失去压缩也不能丢尾消息。
            messages = self._load_all_messages_unlocked(session_dir)
            through_seq = self._journal_last_seq(session_dir)
            return ActiveContext(messages=messages, through_seq=through_seq), 0
        try:
            context = ActiveContext.from_payload(value)
        except ActiveContextError as exc:
            raise ValueError(str(exc)) from exc
        if context.through_seq > self._journal_last_seq(session_dir):
            raise ValueError("会话活动上下文领先于 journal，拒绝跳过历史")
        return context, len(raw)

    def _load_active_context_unlocked(
        self,
        session_dir: Path,
        *,
        repair_incomplete: bool = False,
    ) -> tuple[ActiveContext, int]:
        """持锁读取快照并补齐 journal 尾部，返回 (完整上下文, 最新序号)。

        读取不推进快照修订号：修订号只在实际写回时增加，避免读操作伪造新版本。
        """

        context, checkpoint_bytes = self._read_snapshot_unlocked(session_dir)
        # checkpoint 可以落后于 journal：仅重读 through_seq 之后的消息，避免丢尾或重复。
        tail = self._load_journal_after(
            session_dir,
            context.through_seq,
            max_bytes=self.max_bytes - checkpoint_bytes,
        )
        messages = list(context.messages)
        messages.extend(tail)
        if repair_incomplete:
            messages = self._repair_incomplete_tool_calls(messages)
        if len(messages) > self.max_events:
            raise ValueError(f"会话活动上下文超过消息上限 {self.max_events}")
        # 已把 journal 尾部并入 messages，水位线必须同步到实际吸收位置，
        # 否则返回的上下文再保存时会带着旧水位线，导致下次恢复重复拼接尾部。
        last_seq = self._journal_last_seq(session_dir)
        recovered = ActiveContext(
            messages=messages,
            through_seq=last_seq,
            snapshot_revision=context.snapshot_revision,
            pins=context.pins,
            anchor=context.anchor,
        )
        return recovered, last_seq

    @staticmethod
    def _check_expected_revision(expected_revision: int | None, current_revision: int) -> None:
        """修订号 CAS：比较调用方加载时观察到的快照修订号，冲突时拒绝覆盖。"""
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数或 None")
        if expected_revision != current_revision:
            raise RuntimeError("会话快照已被另一个进程更新；为避免覆盖，请重新加载 session")

    def save_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        messages: list[dict[str, Any]],
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """原子保存压缩后的活动窗口；不修改 context.jsonl 原始历史。

        expected_revision 用于保护水位线不变的原地压缩：与已存修订号不一致时显式失败。
        """

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
            self._save_active_messages_unlocked(
                session_dir, messages, current_seq, expected_revision=expected_revision
            )

    def save_active_context(
        self,
        session_id: str,
        workspace: str | Path,
        context: ActiveContext,
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """原子保存完整活动上下文（含 pins、任务锚与修订号）。

        expected_revision 为调用方加载时观察到的修订号；不一致时显式失败，防止丢失更新。
        """

        if not isinstance(context, ActiveContext):
            raise TypeError("context 必须是 ActiveContext")
        root = self._workspace_root(workspace)
        session_dir = self._session_dir(session_id, root)
        self.load(session_id, root)
        with self._session_write_lock(session_dir):
            # 写入前先校验既有快照版本/格式，避免用新格式覆盖未知字段（fail closed）。
            self._precheck_snapshot_unlocked(session_dir)
            current_seq = self._journal_last_seq(session_dir)
            self._check_expected_seq(expected_seq, current_seq)
            # 拒绝本仓库随后无法读取的快照：超预算或水位线超前于 journal。
            if len(context.messages) > self.max_events:
                raise ValueError(f"活动上下文超过消息上限 {self.max_events}")
            if context.through_seq > current_seq:
                raise ValueError("会话活动上下文领先于 journal，拒绝写入")
            existing = self._read_preserved_snapshot(session_dir)
            self._check_expected_revision(expected_revision, existing.snapshot_revision)
            if (
                expected_revision is not None
                and context.snapshot_revision <= existing.snapshot_revision
            ):
                raise ValueError("保存完整上下文时必须推进 snapshot_revision")
            self._save_active_context_unlocked(session_dir, context)

    def _save_active_messages_unlocked(
        self,
        session_dir: Path,
        messages: list[dict[str, Any]],
        through_seq: int,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """在调用方已持锁时写入消息窗口；保留既有 pins/anchor，避免旧接口擦除新字段。

        旧签名保持稳定，供 journal 追加路径和历史测试沿用；新增字段通过
        _read_preserved_snapshot 读回后在写回时保留，并推进快照修订号。
        """

        existing = self._read_preserved_snapshot(session_dir)
        self._check_expected_revision(expected_revision, existing.snapshot_revision)
        updated = existing.with_messages(list(messages), through_seq)
        if (
            expected_revision is not None
            and updated.snapshot_revision <= existing.snapshot_revision
        ):
            raise ValueError("保存活动窗口时必须推进 snapshot_revision（内容需变化）")
        self._save_active_context_unlocked(session_dir, updated)

    def _read_preserved_snapshot(self, session_dir: Path) -> ActiveContext:
        """读回现有快照以保留 pins/anchor。

        缺失或 v1（list）返回空上下文；未知版本或畸形 v3 **fail closed**，绝不静默覆盖，
        从而保证写入路径不会丢弃旧程序无法理解的新字段。
        """

        path = session_dir / "active_context.json"
        if not path.exists():
            return ActiveContext.empty()
        self._reject_symlink(path)
        try:
            raw = path.read_bytes()
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
        if isinstance(value, list):
            # v1 没有 pins/anchor，保留字段按空处理。
            return ActiveContext.empty()
        try:
            return ActiveContext.from_payload(value)
        except ActiveContextError as exc:
            raise ValueError(str(exc)) from exc

    def _precheck_snapshot_unlocked(self, session_dir: Path) -> None:
        """在任何写入前校验既有快照版本/格式；未知或畸形直接失败，避免先写 journal 再踩坏快照。"""

        self._read_preserved_snapshot(session_dir)

    def _save_active_context_unlocked(self, session_dir: Path, context: ActiveContext) -> None:
        """在调用方已持锁时写入脱敏后的当前版本快照。"""

        redacted = ActiveContext(
            messages=redact(context.messages),
            through_seq=context.through_seq,
            snapshot_revision=context.snapshot_revision,
            pins=tuple(
                Pin(
                    pin_id=pin.pin_id,
                    text=redact(pin.text),
                    seq=pin.seq,
                    created_at=pin.created_at,
                )
                for pin in context.pins
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
        )
        encoded = json.dumps(
            redacted.to_payload(),
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
