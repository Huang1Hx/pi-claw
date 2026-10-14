"""会话持久化端口：文件后端与 SQLite 后端共享的调用契约。

这里只声明行为接口，不承担路径校验、脱敏或权限判断；这些仍是各实现与上层门面的职责。
调用方（CLI/REPL/commands）只依赖本协议，从而可以在 files 与 sqlite 之间切换而无需改动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.active_context import ActiveContext
from persistence.models import SessionInfo


@runtime_checkable
class SessionRepository(Protocol):
    """会话事件、活动快照、元数据与用量的统一访问接口。"""

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
        ...

    def latest(self, workspace: str | Path) -> SessionInfo | None:
        """返回当前工作区最近更新的会话。"""
        ...

    def load(self, session_id: str, workspace: str | Path) -> SessionInfo:
        """校验会话身份与工作区归属并恢复元数据。"""
        ...

    def list_sessions(self, workspace: str | Path) -> list[SessionInfo]:
        """列出当前工作区合法会话，按更新时间排序。"""
        ...

    def update_state(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        title: str | None = None,
        approved_tools: list[str] | None = None,
    ) -> SessionInfo:
        """更新标题或审批 scope。"""
        ...

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
        ...

    def append_message(
        self,
        session_id: str,
        workspace: str | Path,
        message: dict[str, Any],
        *,
        active_messages: list[dict[str, Any]] | None = None,
        expected_seq: int | None = None,
    ) -> int:
        """追加原始消息并返回新序号；expected_seq 不匹配时拒绝提交。"""
        ...

    def load_messages(
        self,
        session_id: str,
        workspace: str | Path,
    ) -> list[dict[str, Any]]:
        """读取完整原始消息日志。"""
        ...

    def load_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        """读取发给模型的活动窗口。"""
        ...

    def load_active_snapshot(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """返回 (活动窗口, 最新日志序号)；序号用作下一次保存的 expected_seq。"""
        ...

    def load_active_context(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        repair_incomplete: bool = False,
    ) -> ActiveContext:
        """返回含 pins、任务锚与修订号的完整活动上下文。"""
        ...

    def save_active_messages(
        self,
        session_id: str,
        workspace: str | Path,
        messages: list[dict[str, Any]],
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """保存压缩后的活动窗口；不得擦除既有 pins/anchor。

        expected_revision 为加载时观察到的修订号，用于保护水位线不变的原地压缩。
        """
        ...

    def save_active_context(
        self,
        session_id: str,
        workspace: str | Path,
        context: ActiveContext,
        *,
        expected_seq: int | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """保存完整活动上下文（含 pins 与任务锚）。

        expected_revision 为加载时观察到的修订号：与已存修订号不一致时显式失败。

        契约：写入前必须校验既有快照版本/格式，并拒绝超预算或领先 journal 的水位线。
        `snapshot_revision` 目前仅随写入递增用于版本标注，**尚未**作为并发 CAS 依据；
        不追加消息的并发更新（如两次改 pins）暂无法被 `expected_seq` 识别，
        修订号 CAS 的实现在 M2/M4，调用方不得假定当前已有该保护。
        """
        ...

    def record_usage(
        self,
        session_id: str,
        workspace: str | Path,
        run_id: str,
        usage: dict[str, Any],
    ) -> None:
        """按 run_id 幂等记录一次服务端用量。"""
        ...

    def total_usage(self, session_id: str, workspace: str | Path) -> dict[str, int]:
        """聚合单个会话的用量。"""
        ...

    def workspace_usage(self, workspace: str | Path) -> dict[str, Any]:
        """聚合当前工作区所有会话的用量。"""
        ...
