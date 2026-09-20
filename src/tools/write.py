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
        target = self.workspace.resolve(path, allow_missing=True)
        if old == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_create_text(target, new)
            return {"path": str(target.relative_to(self.workspace.root)), "created": True}
        if not target.is_file():
            raise ValueError("精确替换要求目标文件已存在")
        if target.stat().st_size > self.max_write_bytes:
            raise ValueError(f"目标文件超过写入处理上限 {self.max_write_bytes} 字节")
        original_fingerprint = self._sha256_file(target)
        content = target.read_text(encoding="utf-8")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取期间发生变化，拒绝继续")
        count = content.count(old)
        if count == 0:
            raise ValueError("old 文本不存在，未修改文件")
        if count != 1:
            raise ValueError(f"old 文本出现 {count} 次，必须唯一，未修改文件")
        replaced = content.replace(old, new, 1)
        if len(replaced.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"替换结果超过写入上限 {self.max_write_bytes} 字节")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取后发生变化，拒绝覆盖")
        # 指纹检查能发现多数并发编辑，但检查与替换并非原子 CAS，不等于跨进程文件锁。
        self._atomic_replace_text(target, replaced)
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "created": False,
            "replacements": 1,
        }
