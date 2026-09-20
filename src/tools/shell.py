"""argv 子进程执行、超时与凭据环境隔离。"""

from __future__ import annotations

import os
import subprocess
from typing import Any


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
        输出是在捕获之后截断，不是流式内存上限；测试命令仍可执行仓库中的任意代码。
        """
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise TypeError("argv 必须是非空字符串数组")
        resolved_cwd = self.workspace.resolve(cwd)
        effective_timeout = self.command_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0 or effective_timeout > self.command_timeout:
            raise ValueError(f"timeout 必须在 0 到 {self.command_timeout} 秒之间")
        # 传递 argv 数组且不启用 shell，不把模型文本作为 shell 语法解释。
        completed = subprocess.run(
            argv,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
            env=self._restricted_environment(),
        )
        stdout, stdout_truncated = self._truncate(completed.stdout)
        stderr, stderr_truncated = self._truncate(completed.stderr)
        return {
            "argv": argv,
            "cwd": str(resolved_cwd.relative_to(self.workspace.root)) or ".",
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    def _truncate(self, value: str) -> tuple[str, bool]:
        """截短用于展示的命令输出并返回截断标志；不限制 subprocess 捕获时的内存。"""
        if len(value) <= self.max_output_chars:
            return value, False
        return value[: self.max_output_chars] + "\n...[输出已截断]", True

    @staticmethod
    def _restricted_environment() -> dict[str, str]:
        # 仅传递解释器定位、区域设置和临时目录所需字段。采用白名单而非“删除几个
        # 已知字段”，可防止未来新增的 TOKEN/PASSWORD/云凭据意外进入子进程。
        """构造子进程环境白名单，避免默认继承 API Key 等凭据；不等于文件或网络沙箱。"""
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "WINDIR",
            "PATHEXT",
        }
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}
