"""Docker 后端预检；实际隔离命令在 Task 3.6 接入。"""

from __future__ import annotations

import shutil
import subprocess

from config.models import SandboxSettings
from sandbox.models import CommandExecution, CommandRequest


class DockerCommandRunner:
    backend = "docker"

    def __init__(self, settings: SandboxSettings | None = None) -> None:
        self.settings = settings or SandboxSettings(backend="docker")

    def preflight(self) -> None:
        binary = shutil.which("docker")
        if binary is None:
            raise RuntimeError("Docker CLI 不可用")
        probe = subprocess.run(
            [binary, "image", "inspect", self.settings.image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if probe.returncode or not probe.stdout.strip().startswith("sha256:"):
            raise RuntimeError("Docker 镜像不可用或 daemon 未启动")

    def run(self, request: CommandRequest, *, control: object | None = None) -> CommandExecution:
        raise RuntimeError("Docker 隔离后端尚未接入，已拒绝宿主执行")
