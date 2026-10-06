"""基于 root directory fd 的无跟随文件访问与版本绑定原子写入。"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path


class SecureFileViolation(ValueError):
    """文件路径含链接、节点类型不安全、版本变化或平台能力不足。"""


def secure_dir_fd_supported() -> bool:
    """返回当前平台是否具备实现无跟随相对访问所需的最小 POSIX 能力。"""
    required = (os.open, os.stat, os.mkdir, os.unlink, os.link, os.rename)
    return (
        bool(getattr(os, "O_NOFOLLOW", 0))
        and all(function in os.supports_dir_fd for function in required)
        and os.listdir in os.supports_fd
    )


@dataclass(frozen=True)
class SecureFileVersion:
    """审批与最终提交共同使用的不可变文件身份及正文指纹。"""

    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    link_count: int | None = None
    sha256: str | None = None
    anchor_device: int | None = None
    anchor_inode: int | None = None
    missing_parts: tuple[str, ...] = ()

    @classmethod
    def missing(
        cls,
        anchor: os.stat_result,
        missing_parts: tuple[str, ...],
    ) -> SecureFileVersion:
        """把缺失目标绑定到最深已存在目录 inode 与尚未出现的路径片段。"""
        return cls(
            False,
            anchor_device=anchor.st_dev,
            anchor_inode=anchor.st_ino,
            missing_parts=missing_parts,
        )


@dataclass(frozen=True)
class SecureFileSnapshot:
    """一次稳定打开读取到的文件版本和字节正文。"""

    version: SecureFileVersion
    content: bytes
    truncated: bool = False


class SecureWorkspaceFS:
    """所有路径均相对固定工作区 root fd 逐段打开，不跟随任何 symlink。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=True)
        root_metadata = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError("安全文件根目录必须是已存在的目录")
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self.supported = secure_dir_fd_supported()

    def version(
        self,
        relative: Path,
        *,
        allow_missing: bool = False,
        max_bytes: int | None = None,
    ) -> SecureFileVersion:
        """返回稳定普通文件版本；只在明确允许时把缺失路径表示为 missing。"""
        self._require_supported()
        try:
            with self._open_parent(relative) as (parent_descriptor, leaf):
                return self._read_at(
                    parent_descriptor,
                    leaf,
                    max_bytes=max_bytes,
                    truncate=False,
                ).version
        except FileNotFoundError:
            if allow_missing:
                return self._missing_version(relative)
            raise SecureFileViolation(f"文件不存在：{relative.as_posix()}") from None

    def read_full(self, relative: Path, *, max_bytes: int) -> SecureFileSnapshot:
        """完整读取有界普通文件，并在打开、读取后和路径末态三次核对版本。"""
        self._require_supported()
        try:
            with self._open_parent(relative) as (parent_descriptor, leaf):
                return self._read_at(
                    parent_descriptor,
                    leaf,
                    max_bytes=max_bytes,
                    truncate=False,
                )
        except FileNotFoundError:
            raise SecureFileViolation(f"文件不存在：{relative.as_posix()}") from None

    def read_optional_full(self, relative: Path, *, max_bytes: int) -> SecureFileSnapshot:
        """为创建预览读取单一稳定快照；路径缺失时返回显式 missing 版本。"""
        self._require_supported()
        try:
            with self._open_parent(relative) as (parent_descriptor, leaf):
                try:
                    return self._read_at(
                        parent_descriptor,
                        leaf,
                        max_bytes=max_bytes,
                        truncate=False,
                    )
                except FileNotFoundError:
                    anchor = os.fstat(parent_descriptor)
                    return SecureFileSnapshot(
                        SecureFileVersion.missing(anchor, (leaf,)),
                        b"",
                    )
        except FileNotFoundError:
            return SecureFileSnapshot(self._missing_version(relative), b"")

    def read_limited(self, relative: Path, *, max_bytes: int) -> SecureFileSnapshot:
        """最多读取上限加一字节以标记截断，同时验证路径和打开 fd 未发生变化。"""
        self._require_supported()
        try:
            with self._open_parent(relative) as (parent_descriptor, leaf):
                return self._read_at(
                    parent_descriptor,
                    leaf,
                    max_bytes=max_bytes,
                    truncate=True,
                )
        except FileNotFoundError:
            raise SecureFileViolation(f"文件不存在：{relative.as_posix()}") from None

    def regular_file_size(self, relative: Path) -> int:
        """安全打开并验证普通文件，只读取一字节即可返回稳定的目录项大小。"""
        self._require_supported()
        try:
            with self._open_parent(relative) as (parent_descriptor, leaf):
                snapshot = self._read_at(
                    parent_descriptor,
                    leaf,
                    max_bytes=0,
                    truncate=True,
                )
        except FileNotFoundError:
            raise SecureFileViolation(f"文件不存在：{relative.as_posix()}") from None
        assert snapshot.version.size is not None
        return snapshot.version.size

    def list_regular_files(
        self,
        relative: Path,
        *,
        max_entries: int,
        ignored_directory_names: frozenset[str] = frozenset(),
        ignored_file_names: frozenset[str] = frozenset(),
        excluded: Callable[[Path], bool] | None = None,
    ) -> tuple[list[Path], bool]:
        """从 root fd 枚举普通单链接文件，并在返回前复核访问过的目录映射。"""
        self._require_supported()
        if max_entries <= 0:
            raise ValueError("安全枚举上限必须大于 0")
        raw = Path(relative)
        if raw.is_absolute() or ".." in raw.parts:
            raise SecureFileViolation("安全文件枚举只接受工作区相对路径")
        parts = tuple(part for part in raw.parts if part not in {"", "."})
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | os.O_NOFOLLOW
        )
        descriptors: list[int] = []
        visited: list[tuple[Path, tuple[int, int]]] = []
        found: list[Path] = []
        truncated = False

        def walk(directory_descriptor: int, prefix: Path) -> None:
            """递归只持有已验证目录 fd；达到文件或目录预算时停止。"""
            nonlocal truncated
            directory_metadata = os.fstat(directory_descriptor)
            visited.append((prefix, (directory_metadata.st_dev, directory_metadata.st_ino)))
            if len(visited) > max_entries:
                truncated = True
                return
            try:
                names = sorted(os.listdir(directory_descriptor))
            except OSError as exc:
                raise SecureFileViolation("无法安全枚举工作区目录") from exc
            for name in names:
                metadata = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                child_relative = prefix / name if prefix.parts else Path(name)
                if excluded is not None and excluded(child_relative):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if name in ignored_directory_names:
                        continue
                    child = self._open_verified_directory(
                        directory_descriptor,
                        name,
                        metadata,
                        flags,
                    )
                    try:
                        walk(child, child_relative)
                    finally:
                        os.close(child)
                    if truncated:
                        return
                    continue
                if not stat.S_ISREG(metadata.st_mode) or name in ignored_file_names:
                    continue
                if metadata.st_nlink != 1:
                    continue
                self._read_at(
                    directory_descriptor,
                    name,
                    max_bytes=0,
                    truncate=True,
                )
                found.append(child_relative)
                if len(found) >= max_entries:
                    truncated = True
                    return

        try:
            root_descriptor = os.open(self.root, flags)
            descriptors.append(root_descriptor)
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
                raise SecureFileViolation("工作区根目录在初始化后发生变化")
            current = root_descriptor
            prefix = Path()
            for index, name in enumerate(parts):
                metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise SecureFileViolation(f"拒绝中间符号链接：{name}")
                prefix = prefix / name
                is_last = index == len(parts) - 1
                if is_last and stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        raise SecureFileViolation(f"拒绝多重硬链接文件：{name}")
                    self._read_at(current, name, max_bytes=0, truncate=True)
                    return [prefix], False
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SecureFileViolation(f"枚举路径不是普通文件或目录：{name}")
                child = self._open_verified_directory(current, name, metadata, flags)
                descriptors.append(child)
                current = child
            walk(current, prefix)
            for directory, identity in visited:
                probe = directory / ".pi-claw-directory-identity"
                with self._open_parent(probe) as (descriptor, _leaf):
                    metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) != identity:
                    raise SecureFileViolation("目录映射在安全枚举期间发生变化")
            return found, truncated
        except FileNotFoundError as exc:
            raise SecureFileViolation(f"枚举路径不存在：{relative.as_posix()}") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def atomic_create_text(
        self,
        relative: Path,
        content: str,
        *,
        expected: SecureFileVersion,
    ) -> None:
        """在锚定父目录中独占创建；路径或缺失版本变化时拒绝提交。"""
        self._require_supported()
        if expected.exists:
            raise SecureFileViolation("创建审批版本要求目标不存在")
        with self._open_parent(
            relative,
            create_parents=True,
            expected_missing=expected,
        ) as (parent_descriptor, leaf):
            self._before_final_open(relative)
            self._verify_parent_mapping(relative, parent_descriptor)
            if self._lstat_regular(parent_descriptor, leaf, allow_missing=True) is not None:
                raise SecureFileViolation("目标文件在审批后已经存在，拒绝创建")
            temporary = self._write_temporary_text(parent_descriptor, leaf, content, 0o600)
            try:
                if self._lstat_regular(parent_descriptor, leaf, allow_missing=True) is not None:
                    raise SecureFileViolation("目标文件在提交前已经存在，拒绝创建")
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                os.fsync(parent_descriptor)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_descriptor)

    def atomic_replace_text(
        self,
        relative: Path,
        content: str,
        *,
        expected: SecureFileVersion,
        max_bytes: int,
    ) -> None:
        """有界复核原文件后，在同目录落盘临时文件并原子替换目标。"""
        self._require_supported()
        if max_bytes <= 0:
            raise ValueError("安全替换读取上限必须大于 0")
        if not expected.exists or expected.mode is None:
            raise SecureFileViolation("替换审批版本要求目标文件存在")
        with self._open_parent(relative) as (parent_descriptor, leaf):
            self._before_final_open(relative)
            self._verify_parent_mapping(relative, parent_descriptor)
            current = self._read_at(
                parent_descriptor,
                leaf,
                max_bytes=max_bytes,
                truncate=False,
            )
            if current.version != expected:
                raise SecureFileViolation("目标文件在最终打开前发生变化，拒绝覆盖")
            temporary = self._write_temporary_text(
                parent_descriptor,
                leaf,
                content,
                stat.S_IMODE(expected.mode),
            )
            try:
                # 临时文件落盘后再缩小一次 CAS 窗口；跨进程 rename CAS 并非 POSIX 可移植能力。
                latest = self._read_at(
                    parent_descriptor,
                    leaf,
                    max_bytes=max_bytes,
                    truncate=False,
                )
                if latest.version != expected:
                    raise SecureFileViolation("目标文件在提交前发生变化，拒绝覆盖")
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                os.fsync(parent_descriptor)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_descriptor)

    def _require_supported(self) -> None:
        """能力不足时明确 fail closed，不能退回普通 Path 路径写入。"""
        if not self.supported:
            raise SecureFileViolation(
                "当前平台缺少安全 dir-fd/O_NOFOLLOW 能力，Host 文件工具已 fail closed"
            )

    @contextmanager
    def _open_parent(
        self,
        relative: Path,
        *,
        create_parents: bool = False,
        expected_missing: SecureFileVersion | None = None,
    ) -> Iterator[tuple[int, str]]:
        """从 root fd 逐段打开父目录，任何中间 symlink 或非目录节点都失败。"""
        parts = self._relative_parts(relative)
        descriptors: list[int] = []
        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | os.O_NOFOLLOW
        )
        try:
            root_descriptor = os.open(self.root, root_flags)
            descriptors.append(root_descriptor)
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
                raise SecureFileViolation("工作区根目录在初始化后发生变化")
            current = root_descriptor
            created_from_missing = False
            for index, name in enumerate(parts[:-1]):
                try:
                    metadata = self._lstat_component(current, name)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    if not created_from_missing:
                        self._assert_missing_anchor(
                            expected_missing,
                            os.fstat(current),
                            parts[index:],
                        )
                        created_from_missing = True
                    try:
                        os.mkdir(name, mode=0o755, dir_fd=current)
                    except FileExistsError as exc:
                        raise SecureFileViolation(f"目录节点在创建期间并发出现：{name}") from exc
                    metadata = self._lstat_component(current, name)
                if stat.S_ISLNK(metadata.st_mode):
                    raise SecureFileViolation(f"拒绝中间符号链接：{name}")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SecureFileViolation(f"路径中间节点不是目录：{name}")
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | os.O_NOFOLLOW
                )
                child = self._open_verified_directory(current, name, metadata, flags)
                descriptors.append(child)
                current = child
            if create_parents and not created_from_missing:
                self._assert_missing_anchor(
                    expected_missing,
                    os.fstat(current),
                    (parts[-1],),
                )
            yield current, parts[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _lstat_component(parent_descriptor: int, name: str) -> os.stat_result:
        """相对父 fd 读取单个目录节点，不跟随符号链接。"""
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)

    def _open_verified_directory(
        self,
        parent_descriptor: int,
        name: str,
        expected: os.stat_result,
        flags: int,
    ) -> int:
        """打开并核对目录 inode；open 后任何异常都先关闭尚未托管的 child fd。"""
        try:
            child = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise SecureFileViolation(f"无法安全打开目录节点：{name}") from exc
        try:
            opened = os.fstat(child)
            if not self._same_identity(expected, opened) or not stat.S_ISDIR(opened.st_mode):
                raise SecureFileViolation(f"目录节点在打开期间发生变化：{name}")
        except BaseException:
            os.close(child)
            raise
        return child

    def _missing_version(self, relative: Path) -> SecureFileVersion:
        """稳定定位最深已存在目录，为缺失目标生成可比较的父目录版本。"""
        parts = self._relative_parts(relative)
        descriptors: list[int] = []
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | os.O_NOFOLLOW
        )
        try:
            root_descriptor = os.open(self.root, flags)
            descriptors.append(root_descriptor)
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
                raise SecureFileViolation("工作区根目录在初始化后发生变化")
            current = root_descriptor
            for index, name in enumerate(parts[:-1]):
                try:
                    metadata = self._lstat_component(current, name)
                except FileNotFoundError:
                    return SecureFileVersion.missing(os.fstat(current), parts[index:])
                if stat.S_ISLNK(metadata.st_mode):
                    raise SecureFileViolation(f"拒绝中间符号链接：{name}")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SecureFileViolation(f"路径中间节点不是目录：{name}")
                child = self._open_verified_directory(current, name, metadata, flags)
                descriptors.append(child)
                current = child
            try:
                self._lstat_regular(current, parts[-1])
            except FileNotFoundError:
                return SecureFileVersion.missing(os.fstat(current), (parts[-1],))
            raise SecureFileViolation("目标文件在缺失版本确认期间已经出现")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _assert_missing_anchor(
        expected: SecureFileVersion | None,
        anchor: os.stat_result,
        missing_parts: tuple[str, ...],
    ) -> None:
        """创建路径只能从审批时绑定的目录 inode 和缺失后缀开始。"""
        if (
            expected is None
            or expected.exists
            or expected.anchor_device != anchor.st_dev
            or expected.anchor_inode != anchor.st_ino
            or expected.missing_parts != missing_parts
        ):
            raise SecureFileViolation("缺失目标的父目录在审批后发生变化，拒绝创建")

    def _verify_parent_mapping(self, relative: Path, parent_descriptor: int) -> None:
        """测试钩子执行后从 root 重新打开路径，确认仍映射到同一父目录 inode。"""
        expected = os.fstat(parent_descriptor)
        try:
            with self._open_parent(relative) as (current_descriptor, _leaf):
                current = os.fstat(current_descriptor)
        except (FileNotFoundError, SecureFileViolation) as exc:
            raise SecureFileViolation("父目录在最终打开前发生变化，拒绝写入") from exc
        if not self._same_identity(expected, current):
            raise SecureFileViolation("父目录在最终打开前发生变化，拒绝写入")

    def _read_at(
        self,
        parent_descriptor: int,
        leaf: str,
        *,
        max_bytes: int | None,
        truncate: bool,
    ) -> SecureFileSnapshot:
        """无跟随打开普通文件，读取后同时复核 fd 和目录项版本。"""
        initial = self._lstat_regular(parent_descriptor, leaf)
        assert initial is not None
        if not truncate and max_bytes is not None and initial.st_size > max_bytes:
            raise SecureFileViolation(f"目标文件超过处理上限 {max_bytes} 字节")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise SecureFileViolation(f"无法安全打开文件：{leaf}") from exc
        digest = hashlib.sha256()
        content = bytearray()
        try:
            opened = os.fstat(descriptor)
            self._assert_same_version(initial, opened, leaf)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                if truncate:
                    chunk = stream.read((max_bytes or 0) + 1)
                    content.extend(chunk)
                    digest.update(chunk)
                else:
                    while block := stream.read(65_536):
                        content.extend(block)
                        if max_bytes is not None and len(content) > max_bytes:
                            raise SecureFileViolation(f"目标文件超过处理上限 {max_bytes} 字节")
                        digest.update(block)
            after_read = os.fstat(descriptor)
            self._assert_same_version(initial, after_read, leaf)
        finally:
            os.close(descriptor)
        final = self._lstat_regular(parent_descriptor, leaf)
        assert final is not None
        self._assert_same_version(initial, final, leaf)
        version = self._version_from(
            initial,
            digest.hexdigest(),
            os.fstat(parent_descriptor),
        )
        was_truncated = bool(truncate and max_bytes is not None and len(content) > max_bytes)
        return SecureFileSnapshot(version, bytes(content), was_truncated)

    @staticmethod
    def _lstat_regular(
        parent_descriptor: int,
        leaf: str,
        *,
        allow_missing: bool = False,
    ) -> os.stat_result | None:
        """读取末级目录项且拒绝 symlink、特殊节点与多重硬链接。"""
        try:
            metadata = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        if stat.S_ISLNK(metadata.st_mode):
            raise SecureFileViolation(f"拒绝末级符号链接：{leaf}")
        if not stat.S_ISREG(metadata.st_mode):
            raise SecureFileViolation(f"文件工具只允许普通文件：{leaf}")
        if metadata.st_nlink != 1:
            raise SecureFileViolation(f"拒绝多重硬链接文件：{leaf}")
        return metadata

    @staticmethod
    def _relative_parts(relative: Path) -> tuple[str, ...]:
        """验证调用方提供的是非空安全相对路径。"""
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise SecureFileViolation("安全文件访问只接受工作区相对路径")
        parts = tuple(part for part in path.parts if part not in {"", "."})
        if not parts or any("\x00" in part for part in parts):
            raise SecureFileViolation("安全文件路径不能为空或包含 NUL")
        return parts

    @staticmethod
    def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
        return first.st_dev == second.st_dev and first.st_ino == second.st_ino

    @classmethod
    def _assert_same_version(
        cls,
        first: os.stat_result,
        second: os.stat_result,
        leaf: str,
    ) -> None:
        """核对身份、类型、链接数、大小和纳秒时间，发现竞态立即失败。"""
        if not (
            cls._same_identity(first, second)
            and stat.S_ISREG(second.st_mode)
            and first.st_nlink == second.st_nlink == 1
            and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
        ):
            raise SecureFileViolation(f"文件在安全读取期间发生变化：{leaf}")

    @staticmethod
    def _version_from(
        metadata: os.stat_result,
        digest: str,
        parent: os.stat_result,
    ) -> SecureFileVersion:
        return SecureFileVersion(
            True,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_nlink,
            digest,
            parent.st_dev,
            parent.st_ino,
        )

    @staticmethod
    def _write_temporary_text(
        parent_descriptor: int,
        leaf: str,
        content: str,
        mode: int,
    ) -> str:
        """在锚定父目录内独占创建并 fsync 临时文件，只返回相对临时名称。"""
        for _attempt in range(20):
            temporary = f".{leaf[:32]}-{secrets.token_hex(8)}.tmp"
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
            )
            try:
                descriptor = os.open(temporary, flags, mode, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="", closefd=False
                ) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(descriptor)
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_descriptor)
                raise
            finally:
                os.close(descriptor)
            return temporary
        raise SecureFileViolation("无法创建安全的同目录临时文件")

    def _before_final_open(self, _relative: Path) -> None:
        """竞态回归测试钩子；生产实现不修改文件系统。"""
