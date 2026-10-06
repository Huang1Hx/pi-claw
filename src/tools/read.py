"""有界读取工作区文本。"""

from __future__ import annotations

from typing import Any


class ReadTools:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    def read_file(self, path: str) -> dict[str, Any]:
        """有界读取工作区 UTF-8 文本并标记截断。"""
        relative = self._relative_path(path)
        snapshot = self.workspace.secure_fs.read_limited(
            relative,
            max_bytes=self.max_read_bytes,
        )
        content = snapshot.content[: self.max_read_bytes].decode("utf-8", errors="replace")
        return {
            "path": relative.as_posix(),
            "content": content,
            "truncated": snapshot.truncated,
            "reason": (
                f"读取量达到上限 {self.max_read_bytes} 字节" if snapshot.truncated else None
            ),
        }
