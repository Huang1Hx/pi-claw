"""有界读取工作区文本。"""

from __future__ import annotations

from typing import Any


class ReadTools:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    def read_file(self, path: str) -> dict[str, Any]:
        """有界读取工作区 UTF-8 文本并标记截断。"""
        target = self.workspace.resolve(path)
        if not target.is_file():
            raise ValueError("path 必须是已存在的文件")
        with target.open("rb") as stream:
            raw = stream.read(self.max_read_bytes + 1)
        truncated = len(raw) > self.max_read_bytes
        content = raw[: self.max_read_bytes].decode("utf-8", errors="replace")
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "content": content,
            "truncated": truncated,
            "reason": f"读取量达到上限 {self.max_read_bytes} 字节" if truncated else None,
        }
