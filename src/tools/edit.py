"""先完整校验再原子写回的多段编辑。"""

from __future__ import annotations

from typing import Any


class EditTools:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    def apply_patch(self, path: str, edits: list[dict[str, str]]) -> dict[str, Any]:
        """先在内存中完成全部精确替换，再一次写回目标文件。

        该协议比让模型提交任意 unified diff 更容易校验。任一 ``old`` 不存在或
        不唯一时，整个调用失败且文件保持不变。
        edits 按顺序作用于内存中的新正文，后一个 old 匹配的是前一步替换后的结果。
        必须经 ToolExecutor.execute 进入；本方法本身不请求人工审批。
        """

        if not isinstance(path, str):
            raise TypeError("path 必须是字符串")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise ValueError("edits 必须包含 1 到 50 个精确替换")
        target = self.workspace.resolve(path)
        if not target.is_file():
            raise ValueError("apply_patch 要求目标文件已存在")
        if target.stat().st_size > self.max_write_bytes:
            raise ValueError(f"目标文件超过写入处理上限 {self.max_write_bytes} 字节")

        original_fingerprint = self._sha256_file(target)
        content = target.read_text(encoding="utf-8")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取期间发生变化，拒绝继续")
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
                raise TypeError(f"第 {index} 个 edit 必须且只能包含 old、new")
            old = edit["old"]
            new = edit["new"]
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise ValueError(f"第 {index} 个 edit 的 old 必须为非空字符串")
            count = content.count(old)
            if count != 1:
                raise ValueError(f"第 {index} 个 edit 的 old 出现 {count} 次，必须唯一")
            content = content.replace(old, new, 1)
            if len(content.encode("utf-8")) > self.max_write_bytes:
                raise ValueError(f"补丁结果超过写入上限 {self.max_write_bytes} 字节")

        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取后发生变化，拒绝覆盖")
        # 所有 edits 校验后只提交一次；原子替换防半写，不提供多进程编辑事务。
        self._atomic_replace_text(target, content)
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "edits_applied": len(edits),
        }
