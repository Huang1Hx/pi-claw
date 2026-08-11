"""终端 Coding Agent 的交互审批。

读取类工具由 Policy 直接允许；只有写文件和运行命令会进入这里。审批器不会
改变 Policy 的硬拒绝结果，因此即使启用 ``--yes``，路径越界和明确危险命令
仍然无法执行。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from enum import StrEnum
from typing import TextIO

from .schemas import PolicyDecision, ToolCall
from .trace import redact


class ApprovalChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


StateCallback = Callable[[set[str]], None]
InputFunction = Callable[[str], str]


class TerminalApprovalBroker:
    """提供“允许一次 / 本会话允许同类 / 拒绝”三种选择。"""

    def __init__(
        self,
        *,
        approved_scopes: set[str] | None = None,
        auto_approve: bool = False,
        input_fn: InputFunction = input,
        output: TextIO | None = None,
        interactive: bool | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self.approved_scopes = set(approved_scopes or ())
        self.auto_approve = auto_approve
        self.input_fn = input_fn
        self.output = output or sys.stderr
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.on_state_change = on_state_change

    def __call__(
        self,
        call: ToolCall,
        decision: PolicyDecision,
        preview: str | None = None,
    ) -> bool:
        return self.request(call, decision, preview) is not ApprovalChoice.DENY

    def request(
        self,
        call: ToolCall,
        decision: PolicyDecision,
        preview: str | None = None,
    ) -> ApprovalChoice:
        scope = self.scope_for(call, preview)
        if self.auto_approve:
            return ApprovalChoice.ALLOW_ONCE
        if scope in self.approved_scopes:
            return ApprovalChoice.ALLOW_SESSION
        # 管道/CI 中不能等待输入。只有测试显式注入 input_fn 并设置 interactive
        # 时才会进入交互分支，默认行为始终是拒绝副作用。
        if not self.interactive:
            return ApprovalChoice.DENY

        arguments = self._argument_summary(call)
        safe_preview = redact(preview) if preview else None
        print(
            f"\n需要审批：{call.name}\n原因：{decision.reason}\n参数摘要：{arguments}",
            file=self.output,
        )
        if safe_preview:
            print(f"变更预览：\n{safe_preview}", file=self.output)
        while True:
            answer = self.input_fn("[y] 允许一次 / [a] 本会话允许同类 / [n] 拒绝：").strip().lower()
            if answer in {"y", "yes", "1"}:
                return ApprovalChoice.ALLOW_ONCE
            if answer in {"a", "always", "2"}:
                self.approved_scopes.add(scope)
                if self.on_state_change is not None:
                    self.on_state_change(set(self.approved_scopes))
                return ApprovalChoice.ALLOW_SESSION
            if answer in {"", "n", "no", "3"}:
                return ApprovalChoice.DENY
            print("请输入 y、a 或 n。", file=self.output)

    @staticmethod
    def scope_for(call: ToolCall, preview: str | None = None) -> str:
        """把批准限制在可理解的操作类型，而不是放行所有未来动作。"""

        if call.name in {"write_or_replace", "apply_patch"}:
            path = str(call.arguments.get("path", ""))
            normalized = {
                "arguments": call.arguments,
                # 写审批绑定用户本次看到的完整预览；预览含目标文件 preimage hash。
                # 文件任何位置发生变化后都会形成新 scope，必须重新确认。
                "preview": preview,
            }
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()[:16]
            return f"{call.name}:{path}:{digest}"
        if call.name == "run_command":
            normalized = {
                "argv": call.arguments.get("argv", []),
                "cwd": call.arguments.get("cwd", "."),
            }
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()[:16]
            return f"run_command:{digest}"
        return call.name

    @staticmethod
    def _argument_summary(call: ToolCall) -> str:
        if call.name in {"write_or_replace", "apply_patch"}:
            summary: dict[str, object] = {"path": call.arguments.get("path", "")}
            if call.name == "apply_patch":
                edits = call.arguments.get("edits")
                summary["edit_count"] = len(edits) if isinstance(edits, list) else "invalid"
            return json.dumps(redact(summary), ensure_ascii=False)
        if call.name == "run_command":
            summary = {
                "argv": call.arguments.get("argv", []),
                "cwd": call.arguments.get("cwd", "."),
            }
            return json.dumps(redact(summary), ensure_ascii=False)
        return json.dumps(redact(call.arguments), ensure_ascii=False)
