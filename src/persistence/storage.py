"""耐久原子写入与文件路径保护。"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


class StorageMixin:
    """SessionStore 内部协作组件，共享会话路径和读取预算。"""

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """同步目录项，使原子重命名等目录变更尽可能耐久，并确保关闭描述符。"""
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        """拒绝会话文件符号链接，避免读写被重定向到其他位置。"""
        if path.is_symlink():
            raise ValueError(f"会话文件不得是符号链接：{path.name}")

    @staticmethod
    def _atomic_write(path: Path, encoded: bytes) -> None:
        """将字节先写入同目录临时文件并同步，再原子替换目标；异常时清理临时文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            path.chmod(0o600)
            StorageMixin._fsync_directory(path.parent)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise
