"""终端 Coding Agent 的交互审批。

读取类工具由 Policy 直接允许；只有写文件和运行命令会进入这里。审批器不会
改变 Policy 的硬拒绝结果，因此即使启用 ``--yes``，路径越界和明确危险命令
仍然无法执行。
"""

from __future__ import annotations

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

    def __call__(self, call: ToolCall, decision: PolicyDecision) -> bool:
        return self.request(call, decision) is not ApprovalChoice.DENY

    def request(self, call: ToolCall, decision: PolicyDecision) -> ApprovalChoice:
        scope = self.scope_for(call)
        if self.auto_approve:
            return ApprovalChoice.ALLOW_ONCE
        if scope in self.approved_scopes:
            return ApprovalChoice.ALLOW_SESSION
        # 管道/CI 中不能等待输入。只有测试显式注入 input_fn 并设置 interactive
        # 时才会进入交互分支，默认行为始终是拒绝副作用。
        if not self.interactive:
            return ApprovalChoice.DENY

        preview = json.dumps(redact(call.arguments), ensure_ascii=False, indent=2)
        print(
            f"\n需要审批：{call.name}\n原因：{decision.reason}\n参数：{preview}", file=self.output
        )
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
    def scope_for(call: ToolCall) -> str:
        """把批准限制在可理解的操作类型，而不是放行所有未来动作。"""

        if call.name in {"write_or_replace", "apply_patch"}:
            path = str(call.arguments.get("path", ""))
            return f"{call.name}:{path}"
        if call.name == "run_command":
            argv = call.arguments.get("argv", [])
            if isinstance(argv, list):
                command = ":".join(str(item) for item in argv[:3])
                return f"run_command:{command}"
        return call.name
