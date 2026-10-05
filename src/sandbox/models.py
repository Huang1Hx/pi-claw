"""受信执行器提交的 argv 请求和有界执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CommandRequest:
    argv: list[str]
    cwd: Path
    timeout: float
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, list)
            or not self.argv
            or not all(isinstance(item, str) for item in self.argv)
        ):
            raise TypeError("argv 必须是非空字符串数组")
        if not isinstance(self.cwd, Path):
            raise TypeError("cwd 必须是 Path")
        if self.workspace_root is not None and not isinstance(self.workspace_root, Path):
            raise TypeError("workspace_root 必须是 Path 或 None")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout 必须大于零")


@dataclass(frozen=True)
class CommandExecution:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool = False
    cleanup_failed: bool = False
    cancelled: bool = False
    backend: str = "host"
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)
