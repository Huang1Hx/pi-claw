"""命令执行后端；Docker 模式无法预检时必须拒绝。"""

from config.models import SandboxSettings
from sandbox.docker import DockerCommandRunner
from sandbox.host import HostCommandRunner
from sandbox.models import CommandExecution, CommandRequest


def create_runner(settings: SandboxSettings, *, max_output_chars: int = 100_000):
    """选择唯一命令后端；Docker 失败时不降级到 Host。"""
    if settings.backend == "docker":
        runner = DockerCommandRunner(settings)
        runner.preflight()
        return runner
    return HostCommandRunner(max_output_chars=max_output_chars)


__all__ = [
    "CommandExecution",
    "CommandRequest",
    "DockerCommandRunner",
    "HostCommandRunner",
    "create_runner",
]
