"""独占创建和唯一文本替换。"""

from __future__ import annotations

from typing import Any


class WriteTools:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    def write_or_replace(self, path: str, old: str, new: str) -> dict[str, Any]:
        """独占创建或唯一精确替换；写回前复核文件版本。

        old 为空表示创建新文件，已有目标不会覆盖；非空则必须在现有正文中恰好匹配一次。
        new 是 UTF-8 文本，按字节预算检查；返回路径及创建/替换数量，失败抛出异常。
        该方法由 ToolExecutor.execute 调用，单独调用它不会自动触发审批。
        """
        if not all(isinstance(value, str) for value in (path, old, new)):
            raise TypeError("path、old、new 都必须是字符串")
        if len(old.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"old 超过写入处理上限 {self.max_write_bytes} 字节")
        if len(new.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"new 超过写入上限 {self.max_write_bytes} 字节")
        relative = self._relative_path(path)
        if old == "":
            expected = self.workspace.secure_fs.version(relative, allow_missing=True)
            self._require_approved_version(expected)
            if expected.exists:
                raise ValueError("创建目标已经存在")
            self.workspace.secure_fs.atomic_create_text(relative, new, expected=expected)
            return {"path": relative.as_posix(), "created": True}
        snapshot = self._read_full(relative)
        self._require_approved_version(snapshot.version)
        content = snapshot.content.decode("utf-8")
        count = content.count(old)
        if count == 0:
            raise ValueError("old 文本不存在，未修改文件")
        if count != 1:
            raise ValueError(f"old 文本出现 {count} 次，必须唯一，未修改文件")
        replaced = content.replace(old, new, 1)
        if len(replaced.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"替换结果超过写入上限 {self.max_write_bytes} 字节")
        self.workspace.secure_fs.atomic_replace_text(
            relative,
            replaced,
            expected=snapshot.version,
            max_bytes=self.max_write_bytes,
        )
        return {
            "path": relative.as_posix(),
            "created": False,
            "replacements": 1,
        }
