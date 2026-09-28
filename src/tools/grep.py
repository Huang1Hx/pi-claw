"""有界目录遍历和字面量搜索。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from security.workspace import WorkspaceViolation

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
        base = self.workspace.resolve(path)
        if base.is_file():
            return {
                "files": [str(base.relative_to(self.workspace.root))],
                "truncated": False,
                "reason": None,
            }
        if not base.is_dir():
            raise ValueError("path 必须是文件或目录")
        found: list[str] = []
        for safe in self._iter_files(base):
            found.append(str(safe.relative_to(self.workspace.root)))
            if len(found) >= self.max_files:
                return {
                    "files": found,
                    "truncated": True,
                    "reason": f"文件数达到上限 {self.max_files}",
                }
        return {"files": found, "truncated": False, "reason": None}

    def search_text(self, query: str, path: str = ".") -> dict[str, Any]:
        """按文件数、扫描字节数和匹配数预算执行字面量搜索。"""
        if not isinstance(query, str) or not query:
            raise ValueError("query 必须是非空字符串")
        base = self.workspace.resolve(path)
        candidates = [base] if base.is_file() else self._iter_files(base)
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        skipped_large_files = 0
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                safe = self.workspace.resolve(candidate)
                if scanned_files >= self.max_files:
                    return self._search_result(
                        matches,
                        scanned_files,
                        scanned_bytes,
                        skipped_large_files,
                        f"检查文件数达到上限 {self.max_files}",
                    )
                # 大文件即使被跳过也计入检查文件数，避免海量大文件造成无界遍历。
                scanned_files += 1
                size = safe.stat().st_size
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
                text = safe.read_text(encoding="utf-8")
            except (WorkspaceViolation, UnicodeDecodeError, OSError):
                continue
            scanned_bytes += size
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {
                            "path": str(safe.relative_to(self.workspace.root)),
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
        return self._search_result(matches, scanned_files, scanned_bytes, skipped_large_files)

    def _iter_files(self, base: Path):
        """逐个产出安全文件，避免 ``sorted(rglob())`` 先把整个仓库载入内存。"""

        for current, directory_names, file_names in os.walk(base, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                if name in _IGNORED_DIRECTORY_NAMES:
                    continue
                try:
                    resolved = self.workspace.resolve(current_path / name)
                except WorkspaceViolation:
                    continue
                if resolved.is_dir():
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                if name in _IGNORED_FILE_NAMES:
                    continue
                candidate = current_path / name
                try:
                    resolved = self.workspace.resolve(candidate)
                except WorkspaceViolation:
                    continue
                if resolved.is_file():
                    yield resolved

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
