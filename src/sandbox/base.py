"""命令后端协议。"""

from __future__ import annotations

from typing import Protocol

from sandbox.models import CommandExecution, CommandRequest


class CommandRunner(Protocol):
    def run(self, request: CommandRequest, *, control: object | None = None) -> CommandExecution:
        """执行已通过 Policy/Approval 的请求。"""
