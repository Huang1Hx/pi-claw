"""会话元数据、文件名约束与时间格式。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_SEGMENT_PATTERN = re.compile(r"^context-(\d{20})\.jsonl$")


def _now() -> str:
    """生成带 UTC 时区的 ISO 时间字符串，用于会话创建和更新时间。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """可恢复的会话元数据；不包含 API key。"""

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
        """转换为可序列化的数据对象。"""
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
        """从持久化或协议对象恢复并校验数据。"""
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
