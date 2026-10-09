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
        relative = self._relative_path(path)
        snapshot = self._read_full(relative)
        self._require_approved_version(snapshot.version)
        content = snapshot.content.decode("utf-8")
        preimage = content
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

        self.workspace.secure_fs.atomic_replace_text(
            relative,
            content,
            expected=snapshot.version,
            max_bytes=self.max_write_bytes,
        )
        self._record_write(
            relative,
            kind="replace",
            preimage=preimage,
            pre_version=snapshot.version,
        )
        return {
            "path": relative.as_posix(),
            "edits_applied": len(edits),
        }
