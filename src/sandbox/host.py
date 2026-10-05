"""可信工作区兼容后端；提供有界输出与完整进程组停止语义。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import BinaryIO

from core.control import ExecutionCancelled, ExecutionControl, ExecutionDeadlineExceeded
from sandbox.models import CommandExecution, CommandRequest


@dataclass
class _BoundedBytes:
    """持续排空管道但只在内存中保留固定字节数。"""

    limit: int
    value: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        """保留前缀并记录是否丢弃了超限字节。"""
        remaining = self.limit - len(self.value)
        if remaining > 0:
            self.value.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def text(self) -> str:
        """以容错 UTF-8 解码已保留内容，并给截断结果加明确标记。"""
        decoded = self.value.decode("utf-8", errors="replace")
        return decoded + ("\n...[输出已截断]" if self.truncated else "")


class HostCommandRunner:
    """在宿主进程组中执行已批准命令；仅用于用户信任的工作区。"""

    backend = "host"

    def __init__(
        self,
        *,
        max_output_chars: int = 100_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if max_output_chars <= 0:
            raise ValueError("max_output_chars 必须大于 0")
        self.max_output_chars = max_output_chars
        # 仅受信调用方可固定子进程环境；普通 Host 命令仍使用下面的最小白名单。
        self._environment = dict(environment) if environment is not None else None

    def run(
        self,
        request: CommandRequest,
        *,
        control: ExecutionControl | None = None,
    ) -> CommandExecution:
        """并发排空输出；超时或取消时清理整个进程组后再返回或抛出。"""
        if control is not None:
            control.checkpoint()
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
        environment = (
            self._environment
            if self._environment is not None
            else {key: value for key, value in os.environ.items() if key.upper() in allowed}
        )
        process = subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=environment,
            start_new_session=True,
        )
        stdout = _BoundedBytes(self.max_output_chars)
        stderr = _BoundedBytes(self.max_output_chars)
        readers = [
            threading.Thread(
                target=self._drain,
                args=(process.stdout, stdout),
                name="pi-claw-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, stderr),
                name="pi-claw-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + request.timeout
        timed_out = False
        cleanup_failed = False
        stopped: ExecutionCancelled | ExecutionDeadlineExceeded | None = None
        interrupted: BaseException | None = None
        try:
            while process.poll() is None:
                if control is not None:
                    control.checkpoint()
                if time.monotonic() >= deadline:
                    timed_out = True
                    cleanup_failed = self._terminate_group(process)
                    break
                time.sleep(0.02)
        except (ExecutionCancelled, ExecutionDeadlineExceeded) as exc:
            stopped = exc
            cleanup_failed = self._terminate_group(process)
        except BaseException as exc:
            # Ctrl+C 等宿主异常也必须等统一的 finally 清理完成后再抛出，
            # 这样调用方能同时看到中断原因与最终清理结果。
            interrupted = exc
            cleanup_failed = self._terminate_group(process)
        finally:
            # 直属进程即使正常退出，仍可能留下继承同一 PGID 和管道的后台子进程。
            # 每条退出路径都验证并清理进程组，不能只观察 Popen 的直接子进程。
            process.poll()
            if process.poll() is None or self._group_exists(process.pid):
                cleanup_failed = self._terminate_group(process) or cleanup_failed
            for reader in readers:
                reader.join(timeout=1)
            if any(reader.is_alive() for reader in readers):
                cleanup_failed = True
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        if interrupted is not None:
            if isinstance(interrupted, KeyboardInterrupt):
                interrupted.cleanup_failed = cleanup_failed
            raise interrupted
        if stopped is not None:
            stopped.cleanup_failed = cleanup_failed
            raise stopped
        return CommandExecution(
            process.returncode if process.returncode is not None else -signal.SIGKILL,
            stdout.text(),
            stderr.text(),
            stdout.truncated or stderr.truncated,
            timed_out=timed_out,
            cleanup_failed=cleanup_failed,
            backend=self.backend,
        )

    @staticmethod
    def _drain(pipe: BinaryIO | None, buffer: _BoundedBytes) -> None:
        """读取到 EOF，避免未保留的输出反压并阻塞子进程。"""
        if pipe is None:
            return
        try:
            while chunk := pipe.read(65_536):
                buffer.append(chunk)
        finally:
            pipe.close()

    @staticmethod
    def _group_exists(process_group: int) -> bool:
        """使用信号 0 探测进程组；权限不足也视为仍存在。"""
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _terminate_group(cls, process: subprocess.Popen[bytes]) -> bool:
        """先 TERM，最多等待两秒；残留成员再 KILL，并报告是否仍未清理。"""
        group = process.pid
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGTERM)
        grace_end = time.monotonic() + 2.0
        while cls._group_exists(group) and time.monotonic() < grace_end:
            # poll 会回收已退出的直属子进程，避免把僵尸误判为仍需 SIGKILL。
            process.poll()
            time.sleep(0.02)
        if cls._group_exists(group):
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return True
        check_end = time.monotonic() + 0.2
        while cls._group_exists(group) and time.monotonic() < check_end:
            time.sleep(0.01)
        return cls._group_exists(group)
