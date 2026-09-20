"""文件版本指纹和耐久原子写入。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import suppress
from pathlib import Path


class FileStorage:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    @staticmethod
    def _sha256_file(target: Path) -> str:
        """分块计算文件正文指纹，避免一次载入整文件；用于发现审批或编辑期间的版本变化。"""
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(65_536):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_temporary_text(target: Path, content: str) -> Path:
        """在目标同目录写入并同步 UTF-8 临时文件，成功返回其路径，异常则清理。"""
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise
        return temporary

    @classmethod
    def _atomic_create_text(cls, target: Path, content: str) -> None:
        """用同目录临时文件 + 硬链接实现不覆盖已有目标的原子创建。"""

        temporary = cls._write_temporary_text(target, content)
        try:
            os.link(temporary, target)
            cls._fsync_directory(target.parent)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @classmethod
    def _atomic_replace_text(cls, target: Path, content: str) -> None:
        """完整落盘并 fsync 后再替换，避免进程中断留下半个目标文件。"""

        original_mode = target.stat().st_mode & 0o777
        temporary = cls._write_temporary_text(target, content)
        try:
            temporary.chmod(original_mode)
            os.replace(temporary, target)
            cls._fsync_directory(target.parent)
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """同步创建或替换操作涉及的父目录项，并在退出时关闭文件描述符。"""
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
