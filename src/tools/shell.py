"""argv 子进程执行、超时与凭据环境隔离。"""

from __future__ import annotations

import sys
from typing import Any

from sandbox.models import CommandRequest


class CommandTools:
    """由 ToolExecutor 绑定工作区和资源预算后使用。"""

    def run_command(
        self,
        argv: list[str],
        cwd: str = ".",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """在受控 cwd 下执行 argv，返回退出码与有界显示输出。

        调用前须由 ToolExecutor 完成命令白名单检查和审批；cwd 相对工作区解析。
        timeout 不能超过宿主上限；非零退出码保留在结果中，由执行器判为失败。
        Host 后端仍可运行仓库代码；流式内存上限与取消由 Runner 管理。
        """
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise TypeError("argv 必须是非空字符串数组")
        resolved_cwd = self.workspace.resolve(cwd)
        effective_timeout = self.command_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0 or effective_timeout > self.command_timeout:
            raise ValueError(f"timeout 必须在 0 到 {self.command_timeout} 秒之间")
        execution = self.command_runner.run(
            CommandRequest(
                argv,
                resolved_cwd,
                effective_timeout,
                workspace_root=self.workspace.root,
            ),
            control=self.execution_control,
        )
        display_argv = ["python", *argv[1:]] if argv[0] == sys.executable else list(argv)
        return {
            "argv": display_argv,
            "cwd": str(resolved_cwd.relative_to(self.workspace.root)) or ".",
            "returncode": execution.returncode,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "truncated": execution.truncated,
            "backend": execution.backend,
            "timed_out": execution.timed_out,
            "cleanup_failed": execution.cleanup_failed,
            "cancelled": execution.cancelled,
            "sandbox": execution.metadata,
        }
