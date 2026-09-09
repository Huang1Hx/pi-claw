"""Pi-Claw 在 Provider、Agent、Policy 与 Tools 之间传递的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PolicyAction(StrEnum):
    """策略对一次工具调用给出的动作。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RunStatus(StrEnum):
    """一次 Agent 运行的终止状态。"""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    PROVIDER_ERROR = "provider_error"
    UNVERIFIED = "unverified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型发出的结构化工具调用；命令不能由自由文本隐式推导。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("ToolCall.id 必须是非空字符串")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("ToolCall.name 必须是非空字符串")
        if not isinstance(self.arguments, dict):
            raise TypeError("ToolCall.arguments 必须是 JSON 对象")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolCall:
        return cls(
            id=value["id"],
            name=value["name"],
            arguments=value.get("arguments", {}),
        )


@dataclass(frozen=True, slots=True)
class ModelReply:
    """模型单轮回复：最终文本，或同一轮的一组结构化工具调用。

    ``tool_call`` 保留给已有调用方；新代码使用 ``tool_calls``。两者会在
    ``__post_init__`` 中归一化，避免项目升级后破坏 v0 的测试和示例。
    """

    final_text: str | None = None
    tool_call: ToolCall | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        has_final = self.final_text is not None
        if self.tool_call is not None and self.tool_calls:
            raise ValueError("ModelReply 不能同时设置 tool_call 和 tool_calls")
        calls = self.tool_calls
        if self.tool_call is not None:
            calls = (self.tool_call,)
            object.__setattr__(self, "tool_calls", calls)
        elif len(calls) == 1:
            object.__setattr__(self, "tool_call", calls[0])
        if not all(isinstance(call, ToolCall) for call in calls):
            raise TypeError("tool_calls 中每项都必须是 ToolCall")
        has_call = bool(calls)
        if has_final == has_call:
            raise ValueError("ModelReply 必须且只能包含 final_text 或 tool_calls")
        if has_final and not isinstance(self.final_text, str):
            raise TypeError("final_text 必须是字符串")

    @classmethod
    def final(cls, text: str) -> ModelReply:
        return cls(final_text=text)

    @classmethod
    def call(cls, call: ToolCall) -> ModelReply:
        return cls(tool_call=call)

    @classmethod
    def calls(cls, calls: tuple[ToolCall, ...] | list[ToolCall]) -> ModelReply:
        return cls(tool_calls=tuple(calls))

    def to_dict(self) -> dict[str, Any]:
        if self.tool_calls:
            if len(self.tool_calls) == 1:
                return {"tool_call": self.tool_calls[0].to_dict()}
            return {"tool_calls": [call.to_dict() for call in self.tool_calls]}
        return {"final_text": self.final_text}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果。策略拒绝也用该类型返回，避免打断 Agent 循环。"""

    call_id: str
    tool_name: str
    ok: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    status: RunStatus
    run_id: str
    steps: int
    final_text: str | None = None
    error: str | None = None
    verification: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "run_id": self.run_id,
            "steps": self.steps,
            "final_text": self.final_text,
            "error": self.error,
            "verification": self.verification,
        }
