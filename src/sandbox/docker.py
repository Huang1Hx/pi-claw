"""Docker 隔离后端：固定镜像与运行参数、临时快照和强制容器清理。"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from config.models import SandboxSettings
from core.control import ExecutionStopped
from sandbox.host import HostCommandRunner
from sandbox.models import CommandExecution, CommandRequest
from sandbox.snapshot import (
    SnapshotCleanupError,
    SnapshotViolation,
    WorkspaceSnapshot,
    WorkspaceSnapshotBuilder,
)
from security.workspace import Workspace


class DockerCleanupError(RuntimeError):
    """Docker 命令失败且隔离资源未能确认清理完成。"""

    backend = "docker"
    cleanup_failed = True


class DockerExecutionError(RuntimeError):
    """Docker 隔离命令启动失败；原异常只保留在本地 cause，不能进入 Trace。"""

    backend = "docker"


class DockerCommandRunner:
    """用受信宿主代码构造 Docker CLI，模型只能提供容器内命令 argv。"""

    backend = "docker"
    _CLIENT_ENVIRONMENT_KEYS = {
        "DOCKER_API_VERSION",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_DEFAULT_PLATFORM",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
    _CLIENT_PATH_KEYS = {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "HOME",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        snapshot_builder: WorkspaceSnapshotBuilder | None = None,
        max_output_chars: int = 100_000,
    ) -> None:
        self.settings = settings or SandboxSettings(backend="docker")
        self.snapshot_builder = snapshot_builder or WorkspaceSnapshotBuilder()
        self._docker_environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self._CLIENT_ENVIRONMENT_KEYS
        }
        client_base = Path.cwd().resolve()
        for key in self._CLIENT_PATH_KEYS & self._docker_environment.keys():
            configured = Path(self._docker_environment[key])
            if not configured.is_absolute():
                self._docker_environment[key] = str((client_base / configured).resolve())
        self._host_runner = HostCommandRunner(
            max_output_chars=max_output_chars,
            environment=self._docker_environment,
        )
        self._docker_binary: str | None = None
        self._image_id: str | None = None

    @property
    def image_id(self) -> str | None:
        """返回预检固定的 daemon image ID；未预检时为 None。"""
        return self._image_id

    def preflight(self) -> None:
        """解析绝对 Docker CLI 和不可变 image ID；失败时不降级到 Host。"""
        binary = shutil.which("docker")
        if binary is None:
            raise RuntimeError("Docker CLI 不可用")
        image_id = self._inspect_image_id(binary)
        if image_id is None:
            raise RuntimeError("Docker 镜像不可用或 daemon 未启动")
        self._docker_binary = str(Path(binary).resolve())
        self._image_id = image_id

    def _inspect_image_id(self, binary: str) -> str | None:
        """读取本地 tag 的 sha256 ID；不 pull、不构建，也不接受模糊输出。"""
        probe = subprocess.run(
            [binary, "image", "inspect", self.settings.image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=self._docker_environment,
        )
        candidate = probe.stdout.strip()
        if probe.returncode or re.fullmatch(r"sha256:[0-9a-f]{64}", candidate) is None:
            return None
        return candidate

    def assert_image_unchanged(self) -> None:
        """每次运行前确认配置 tag 仍指向预检时固定的同一镜像。"""
        if self._docker_binary is None or self._image_id is None:
            raise RuntimeError("Docker 后端尚未预检")
        current = self._inspect_image_id(self._docker_binary)
        if current is None:
            raise RuntimeError("Docker 镜像不可用或 daemon 未启动")
        if current != self._image_id:
            raise RuntimeError("Docker 镜像 tag 在预检后发生漂移，已拒绝运行")

    def prepare_snapshot(self, request: CommandRequest) -> WorkspaceSnapshot:
        """为单次 Docker 命令创建独占快照，并确认 cwd 属于声明的工作区。"""
        if Path(request.argv[0]).name == "uv" and request.argv[1:2] == ["sync"]:
            raise SnapshotViolation("Docker 后端禁止运行时依赖安装：uv sync")
        workspace = Workspace(request.workspace_root or request.cwd)
        workspace.resolve(request.cwd)
        return self.snapshot_builder.build(workspace)

    def build_run_argv(
        self,
        request: CommandRequest,
        snapshot: WorkspaceSnapshot,
        container_name: str,
    ) -> list[str]:
        """构造唯一允许的 Docker argv；模型命令只能位于 image ID 之后。"""
        if self._docker_binary is None or self._image_id is None:
            raise RuntimeError("Docker 后端尚未预检")
        workspace = Workspace(request.workspace_root or request.cwd)
        cwd = workspace.resolve(request.cwd)
        relative_cwd = cwd.relative_to(workspace.root).as_posix() or "."
        if any(character in relative_cwd for character in "\r\n\x00"):
            raise SnapshotViolation("Docker cwd 含不允许的控制字符")
        command = list(request.argv)
        python_names = {"python", "python3", Path(sys.executable).name}
        if Path(command[0]).name in python_names:
            command[0] = "python"
        return [
            self._docker_binary,
            "run",
            f"--name={container_name}",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--init",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            f"--pids-limit={self.settings.pids}",
            f"--memory={self.settings.memory_mib}m",
            f"--memory-swap={self.settings.memory_mib}m",
            f"--cpus={self.settings.cpus}",
            "--user=65532:65532",
            (
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,"
                f"size={self.settings.tmpfs_mib}m,uid=65532,gid=65532,mode=1777"
            ),
            (
                "--tmpfs=/workspace:rw,nosuid,nodev,noexec,"
                f"size={self.settings.workspace_tmpfs_mib}m,uid=65532,gid=65532,mode=0700"
            ),
            f"--mount=type=bind,src={snapshot.path},dst=/input,readonly",
            "--env=HOME=/tmp",
            "--env=PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONPATH=/workspace/src",
            "--env=UV_PROJECT_ENVIRONMENT=/opt/venv",
            "--env=UV_OFFLINE=1",
            "--env=UV_NO_SYNC=1",
            "--env=UV_NO_ENV_FILE=1",
            f"--env=PI_CLAW_CWD_RELATIVE={relative_cwd}",
            self._image_id,
            *command,
        ]

    def run(self, request: CommandRequest, *, control: object | None = None) -> CommandExecution:
        """在一次性容器中运行批准命令，所有退出路径都删除容器与输入快照。"""
        if self._docker_binary is None or self._image_id is None:
            self.preflight()
        self.assert_image_unchanged()
        try:
            snapshot = self.prepare_snapshot(request)
        except (ExecutionStopped, KeyboardInterrupt):
            raise
        except SnapshotCleanupError as exc:
            # 原始异常可能含宿主临时路径；只向 ToolResult 暴露固定摘要和清理状态。
            raise DockerCleanupError("Docker 输入快照构建失败且清理失败") from exc
        except BaseException as exc:
            raise DockerExecutionError("Docker 输入快照构建失败") from exc
        container_name: str | None = None
        execution: CommandExecution | None = None
        pending: BaseException | None = None
        cleanup_failed = False
        cleanup_interrupted: BaseException | None = None
        try:
            container_name = self._new_container_name()
            argv = self.build_run_argv(request, snapshot, container_name)
            execution = self._host_runner.run(
                CommandRequest(argv, snapshot.path.parent, request.timeout),
                control=control,
            )
        except BaseException as exc:
            pending = exc
        finally:
            if container_name is not None:
                try:
                    cleanup_failed = self._remove_container(container_name)
                except BaseException as exc:
                    cleanup_failed = True
                    cleanup_interrupted = exc
            try:
                snapshot.cleanup()
            except SnapshotCleanupError:
                cleanup_failed = True
        if pending is None and cleanup_interrupted is not None:
            pending = cleanup_interrupted
        if pending is not None:
            if isinstance(pending, (ExecutionStopped, KeyboardInterrupt)):
                pending.cleanup_failed = (
                    bool(getattr(pending, "cleanup_failed", False)) or cleanup_failed
                )
            elif cleanup_failed:
                raise DockerCleanupError("Docker 命令失败且沙箱清理失败") from pending
            else:
                raise DockerExecutionError("Docker 隔离命令执行失败") from pending
            raise pending
        if execution is None:
            raise RuntimeError("Docker 命令未产生执行结果")
        metadata: dict[str, str | int | float | bool] = {
            "image_id": self._image_id,
            "network": self.settings.network,
            "mount_count": 1,
            "memory_mib": self.settings.memory_mib,
            "cpus": float(self.settings.cpus),
            "pids": self.settings.pids,
            "snapshot_manifest_sha256": snapshot.manifest_sha256,
            "snapshot_file_count": snapshot.file_count,
            "snapshot_total_bytes": snapshot.total_bytes,
        }
        return replace(
            execution,
            stdout=self._redact_snapshot_paths(execution.stdout, snapshot),
            stderr=self._redact_snapshot_paths(execution.stderr, snapshot),
            cleanup_failed=execution.cleanup_failed or cleanup_failed,
            backend=self.backend,
            metadata=metadata,
        )

    @staticmethod
    def _new_container_name() -> str:
        """使用不可预测名称，避免并发任务冲突或命中攻击者预创建容器。"""
        return f"pi-claw-{secrets.token_hex(12)}"

    def _remove_container(self, container_name: str) -> bool:
        """强制删除容器并确认不存在；daemon 故障不能误报为清理成功。"""
        if self._docker_binary is None:
            return True
        try:
            subprocess.run(
                [self._docker_binary, "container", "rm", "--force", container_name],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
                env=self._docker_environment,
            )
            probe = subprocess.run(
                [self._docker_binary, "container", "inspect", container_name],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
                env=self._docker_environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if probe.returncode == 0:
            return True
        # 只有确认探针自己的响应能证明容器不存在；rm 的文本不能替代二次确认。
        probe_error = probe.stderr.strip().lower()
        confirmed_absent = re.fullmatch(
            r"(?:error(?: response from daemon)?:\s*)?"
            rf"no such (?:container|object):\s*{re.escape(container_name)}",
            probe_error,
        )
        return confirmed_absent is None

    @staticmethod
    def _redact_snapshot_paths(text: str, snapshot: WorkspaceSnapshot) -> str:
        """精确移除 Docker daemon 可能回显的私有宿主快照路径。"""
        replacements = {
            str(snapshot.path): "<sandbox-input>",
            str(snapshot.path.resolve()): "<sandbox-input>",
            str(snapshot.path.parent): "<sandbox-snapshot>",
            str(snapshot.path.parent.resolve()): "<sandbox-snapshot>",
        }
        redacted = text
        for private_path in sorted(replacements, key=len, reverse=True):
            redacted = redacted.replace(private_path, replacements[private_path])
        return redacted
