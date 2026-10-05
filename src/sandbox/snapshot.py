"""为 Docker 命令构建无敏感文件、可核验且有界的临时工作区快照。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from security.workspace import Workspace


class SnapshotViolation(RuntimeError):
    """源工作区含不安全节点、发生竞态或超过快照资源上限。"""


class SnapshotCleanupError(RuntimeError):
    """Pi-Claw 创建的临时快照未能完整删除。"""


def _remove_owned_tree(owned_root: Path, *, message: str) -> None:
    """恢复私有只读目录的所有者写权限，再以 fail-closed 语义删除整棵树。"""
    if not owned_root.exists():
        return
    try:
        for directory, names, _files in os.walk(owned_root, topdown=False):
            for name in names:
                os.chmod(Path(directory) / name, 0o700, follow_symlinks=False)
        os.chmod(owned_root, 0o700, follow_symlinks=False)
        shutil.rmtree(owned_root)
    except OSError as exc:
        raise SnapshotCleanupError(message) from exc


@dataclass
class WorkspaceSnapshot:
    """一次命令独占的只读输入载荷及其不含敏感值的 manifest 摘要。"""

    path: Path
    manifest_sha256: str
    file_count: int
    total_bytes: int
    manifest_path: Path
    _owned_root: Path = field(repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)

    def cleanup(self) -> None:
        """删除整棵私有临时目录；失败必须显式抛出，不能伪装成成功。"""
        if self._cleaned:
            return
        if not self._owned_root.exists():
            self._cleaned = True
            return
        _remove_owned_tree(self._owned_root, message="临时工作区快照清理失败")
        self._cleaned = True

    def __enter__(self) -> WorkspaceSnapshot:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.cleanup()


class WorkspaceSnapshotBuilder:
    """以 lstat/no-follow 语义复制普通文件，并生成确定性 manifest。"""

    def __init__(
        self,
        *,
        max_files: int = 20_000,
        max_total_bytes: int = 128 * 1024**2,
        max_nodes: int = 40_000,
    ) -> None:
        if type(max_files) is not int or max_files <= 0:
            raise ValueError("max_files 必须是正整数")
        if type(max_total_bytes) is not int or max_total_bytes <= 0:
            raise ValueError("max_total_bytes 必须是正整数")
        if type(max_nodes) is not int or max_nodes <= 0:
            raise ValueError("max_nodes 必须是正整数")
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_nodes = max_nodes

    def build(self, source: Workspace) -> WorkspaceSnapshot:
        """创建新快照；任一节点不安全或超限时清理全部临时数据并失败。"""
        if not isinstance(source, Workspace):
            raise TypeError("source 必须是 Workspace")
        owned_root = Path(tempfile.mkdtemp(prefix="pi-claw-snapshot-"))
        payload = owned_root / "input"
        manifest_path = owned_root / "manifest.json"
        files: list[dict[str, str | int]] = []
        excluded: list[dict[str, str]] = []
        state = {"file_count": 0, "total_bytes": 0, "node_count": 0}
        root_descriptor: int | None = None
        try:
            # mkdtemp 之后的所有初始化都必须进入统一清理区，避免半成品目录残留。
            payload.mkdir(mode=0o700)
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_descriptor = os.open(source.root, directory_flags)
            self._copy_directory(
                source,
                root_descriptor,
                Path("."),
                payload,
                files,
                excluded,
                state,
            )
            payload.chmod(0o555)
            manifest: dict[str, Any] = {
                "version": 1,
                "files": sorted(files, key=lambda item: str(item["path"])),
                "excluded": sorted(excluded, key=lambda item: str(item["path"])),
                "summary": {
                    "file_count": state["file_count"],
                    "total_bytes": state["total_bytes"],
                    "excluded_count": len(excluded),
                    "node_count": state["node_count"],
                },
            }
            raw_manifest = (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(raw_manifest)
            manifest_path.chmod(0o600)
            return WorkspaceSnapshot(
                payload,
                hashlib.sha256(raw_manifest).hexdigest(),
                state["file_count"],
                state["total_bytes"],
                manifest_path,
                owned_root,
            )
        except BaseException:
            if root_descriptor is not None:
                os.close(root_descriptor)
                root_descriptor = None
            _remove_owned_tree(owned_root, message="失败快照的临时目录清理失败")
            raise
        finally:
            if root_descriptor is not None:
                os.close(root_descriptor)

    def _copy_directory(
        self,
        workspace: Workspace,
        directory_descriptor: int,
        relative_directory: Path,
        destination: Path,
        files: list[dict[str, str | int]],
        excluded: list[dict[str, str]],
        state: dict[str, int],
    ) -> None:
        """按名称排序遍历一层，先 lstat 判型，再决定排除或复制。"""
        try:
            names = sorted(os.listdir(directory_descriptor))
        except OSError as exc:
            raise SnapshotViolation(f"无法读取快照目录：{relative_directory.as_posix()}") from exc
        for name in names:
            state["node_count"] += 1
            if state["node_count"] > self.max_nodes:
                raise SnapshotViolation(f"快照节点数超过上限 {self.max_nodes}")
            relative = relative_directory / name
            display = relative.as_posix().removeprefix("./")
            source_path = workspace.root / relative
            try:
                metadata = self._lstat_at(directory_descriptor, name)
            except OSError as exc:
                raise SnapshotViolation(f"无法检查快照路径：{display}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SnapshotViolation(f"快照拒绝符号链接：{display}")
            is_directory = stat.S_ISDIR(metadata.st_mode)
            is_regular = stat.S_ISREG(metadata.st_mode)
            if not is_directory and not is_regular:
                raise SnapshotViolation(f"快照拒绝特殊文件：{display}")
            # 硬链接可用无害文件名指向工作区外凭据；快照只接受单链接普通文件。
            if is_regular and metadata.st_nlink != 1:
                raise SnapshotViolation(f"快照拒绝多重硬链接：{display}")
            reason = workspace.snapshot_exclusion_reason(relative)
            if reason is not None:
                excluded.append({"path": display, "reason": reason})
                continue
            target = destination / name
            if is_directory:
                target.mkdir(mode=0o700)
                self._before_descend(relative, source_path)
                child_descriptor = self._open_directory_at(
                    directory_descriptor,
                    name,
                    metadata,
                    display,
                )
                try:
                    self._copy_directory(
                        workspace,
                        child_descriptor,
                        relative,
                        target,
                        files,
                        excluded,
                        state,
                    )
                finally:
                    os.close(child_descriptor)
                target.chmod(0o555)
                continue
            if state["file_count"] + 1 > self.max_files:
                raise SnapshotViolation(f"快照文件数超过上限 {self.max_files}")
            if state["total_bytes"] + metadata.st_size > self.max_total_bytes:
                raise SnapshotViolation(f"快照总字节超过上限 {self.max_total_bytes}")
            size, digest = self._copy_regular_file(
                directory_descriptor,
                name,
                source_path,
                target,
                metadata,
            )
            try:
                final = self._lstat_at(directory_descriptor, name)
            except OSError as exc:
                raise SnapshotViolation(f"文件在复制期间消失：{display}") from exc
            if not self._same_file_version(metadata, final):
                raise SnapshotViolation(f"文件在复制期间发生变化：{display}")
            state["file_count"] += 1
            state["total_bytes"] += size
            files.append({"path": display, "size": size, "sha256": digest})

    def _copy_regular_file(
        self,
        directory_descriptor: int,
        name: str,
        source_path: Path,
        destination: Path,
        initial: os.stat_result,
    ) -> tuple[int, str]:
        """用 O_NOFOLLOW 打开源文件，边复制边计算摘要并限制实际读取字节。"""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        except OSError as exc:
            raise SnapshotViolation(f"无法安全打开快照文件：{source_path.name}") from exc
        digest = hashlib.sha256()
        copied = 0
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not self._same_identity(initial, opened):
                raise SnapshotViolation(f"快照文件打开前发生变化：{source_path.name}")
            with (
                os.fdopen(descriptor, "rb", closefd=False) as source_stream,
                destination.open("xb") as target_stream,
            ):
                while chunk := source_stream.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > initial.st_size or copied > self.max_total_bytes:
                        raise SnapshotViolation(f"快照文件复制时超过声明大小：{source_path.name}")
                    target_stream.write(chunk)
                    digest.update(chunk)
            after_read = os.fstat(descriptor)
            if copied != initial.st_size or not self._same_file_version(initial, after_read):
                raise SnapshotViolation(f"文件在复制期间发生变化：{source_path.name}")
        finally:
            os.close(descriptor)
        executable = stat.S_IMODE(initial.st_mode) & 0o111
        destination.chmod(0o555 if executable else 0o444)
        return copied, digest.hexdigest()

    @staticmethod
    def _lstat_at(directory_descriptor: int, name: str) -> os.stat_result:
        """相对已打开目录读取节点元数据，不解析中间路径或跟随末级 symlink。"""
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)

    @classmethod
    def _open_directory_at(
        cls,
        parent_descriptor: int,
        name: str,
        initial: os.stat_result,
        display: str,
    ) -> int:
        """以 O_NOFOLLOW 打开子目录，并核对它仍是 lstat 看到的同一 inode。"""
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise SnapshotViolation(f"无法安全打开快照目录：{display}") from exc
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not cls._same_identity(initial, opened):
            os.close(descriptor)
            raise SnapshotViolation(f"快照目录打开前发生变化：{display}")
        return descriptor

    def _before_descend(self, _relative: Path, _source_path: Path) -> None:
        """竞态测试钩子；生产路径不修改源目录。"""

    @staticmethod
    def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
        """确认打开的 fd 仍指向 lstat 观察到的同一普通文件。"""
        return first.st_dev == second.st_dev and first.st_ino == second.st_ino

    @classmethod
    def _same_file_version(cls, first: os.stat_result, second: os.stat_result) -> bool:
        """确认身份、链接数、类型、大小和纳秒时间在复制期间均未变化。"""
        return (
            cls._same_identity(first, second)
            and stat.S_ISREG(second.st_mode)
            and first.st_nlink == second.st_nlink == 1
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
        )
