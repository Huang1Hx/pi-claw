"""基于安全 dir-fd 枚举的有界文件列表和字面量搜索。"""

from __future__ import annotations

from typing import Any

from security.secure_fs import SecureFileViolation

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "runs",
        "tmp",
    }
)

_IGNORED_FILE_NAMES = frozenset({".DS_Store", ".coverage"})


class SearchTools:
    """由 ToolExecutor 绑定工作区和资源预算后使用。"""

    def list_files(self, path: str = ".") -> dict[str, Any]:
        """按预算枚举工作区内可访问文件。"""
        relative = self.workspace.lexical_relative(path)
        found, truncated = self.workspace.secure_fs.list_regular_files(
            relative,
            max_entries=self.max_files,
            ignored_directory_names=_IGNORED_DIRECTORY_NAMES,
            ignored_file_names=_IGNORED_FILE_NAMES,
            excluded=self.workspace.is_sensitive_relative,
        )
        return {
            "files": [item.as_posix() for item in found],
            "truncated": truncated,
            "reason": f"安全枚举达到上限 {self.max_files}" if truncated else None,
        }

    def search_text(self, query: str, path: str = ".") -> dict[str, Any]:
        """按文件数、扫描字节数和匹配数预算执行字面量搜索。"""
        if not isinstance(query, str) or not query:
            raise ValueError("query 必须是非空字符串")
        relative = self.workspace.lexical_relative(path)
        candidates, traversal_truncated = self.workspace.secure_fs.list_regular_files(
            relative,
            max_entries=self.max_files,
            ignored_directory_names=_IGNORED_DIRECTORY_NAMES,
            ignored_file_names=_IGNORED_FILE_NAMES,
            excluded=self.workspace.is_sensitive_relative,
        )
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        skipped_large_files = 0
        for candidate in candidates:
            try:
                # 大文件即使被跳过也计入检查文件数，避免海量大文件造成无界遍历。
                scanned_files += 1
                size = self.workspace.secure_fs.regular_file_size(candidate)
                if size > self.max_scan_file_bytes:
                    skipped_large_files += 1
                    continue
                if scanned_bytes + size > self.max_scan_bytes:
                    return self._search_result(
                        matches,
                        scanned_files,
                        scanned_bytes,
                        skipped_large_files,
                        f"总扫描预算达到上限 {self.max_scan_bytes} 字节",
                    )
                snapshot = self.workspace.secure_fs.read_full(
                    candidate,
                    max_bytes=min(
                        self.max_scan_file_bytes,
                        self.max_scan_bytes - scanned_bytes,
                    ),
                )
                text = snapshot.content.decode("utf-8")
                size = snapshot.version.size or 0
            except UnicodeDecodeError:
                continue
            except (SecureFileViolation, OSError) as exc:
                return self._search_result(
                    matches,
                    scanned_files,
                    scanned_bytes,
                    skipped_large_files,
                    f"安全扫描中止：{exc}",
                )
            scanned_bytes += size
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {
                            "path": candidate.as_posix(),
                            "line": line_number,
                            "text": line,
                        }
                    )
                    if len(matches) >= self.max_matches:
                        return self._search_result(
                            matches,
                            scanned_files,
                            scanned_bytes,
                            skipped_large_files,
                            f"匹配数达到上限 {self.max_matches}",
                        )
        reason = f"安全枚举达到上限 {self.max_files}" if traversal_truncated else None
        return self._search_result(
            matches,
            scanned_files,
            scanned_bytes,
            skipped_large_files,
            reason,
        )

    @staticmethod
    def _search_result(
        matches: list[dict[str, Any]],
        scanned_files: int,
        scanned_bytes: int,
        skipped_large_files: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """统一返回匹配项和扫描统计；reason 非空表示受预算限制，不能视为完整扫描。"""
        return {
            "matches": matches,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "skipped_large_files": skipped_large_files,
            "truncated": reason is not None,
            "reason": reason,
        }
