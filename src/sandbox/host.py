"""可信工作区兼容后端；3.4 将接入完整进程组停止语义。"""

from __future__ import annotations

import os
import subprocess

from sandbox.models import CommandExecution, CommandRequest


class HostCommandRunner:
    backend = "host"

    def __init__(self, *, max_output_chars: int = 100_000) -> None:
        self.max_output_chars = max_output_chars

    def run(self, request: CommandRequest, *, control: object | None = None) -> CommandExecution:
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
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        completed = subprocess.run(
            request.argv,
            cwd=request.cwd,
            capture_output=True,
            text=True,
            timeout=request.timeout,
            check=False,
            env=environment,
        )
        stdout, out_cut = self._truncate(completed.stdout)
        stderr, err_cut = self._truncate(completed.stderr)
        return CommandExecution(completed.returncode, stdout, stderr, out_cut or err_cut)

    def _truncate(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_output_chars:
            return value, False
        return value[: self.max_output_chars] + "\n...[输出已截断]", True
