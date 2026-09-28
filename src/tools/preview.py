"""审批前 diff 预览与文件版本指纹。"""

from __future__ import annotations

import difflib
import json

from core.schemas import ToolCall
from security.workspace import WorkspaceViolation


class PreviewTools:
    """由 ToolExecutor 绑定工作区和资源预算后使用。"""

    def preview(self, call: ToolCall, *, max_chars: int = 12_000) -> str | None:
        """为副作用调用生成有界预览；预览失败不会绕过后续真实校验。"""

        if max_chars <= 0:
            raise ValueError("preview max_chars 必须大于 0")
        try:
            if call.name == "run_command":
                argv = call.arguments.get("argv", [])
                cwd = call.arguments.get("cwd", ".")
                return self._limit_preview(
                    f"cwd: {cwd}\nargv: {json.dumps(argv, ensure_ascii=False)}",
                    max_chars,
                )
            if call.name not in {"write_or_replace", "apply_patch"}:
                return None
            path = call.arguments.get("path")
            if not isinstance(path, str):
                return "无法预览：path 不是字符串"
            target = self.workspace.resolve(path, allow_missing=True)
            if target.is_file() and target.stat().st_size > self.max_write_bytes:
                return f"无法预览：目标文件超过上限 {self.max_write_bytes} 字节"
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = before
            if call.name == "write_or_replace":
                old = call.arguments.get("old")
                new = call.arguments.get("new")
                if not isinstance(old, str) or not isinstance(new, str):
                    return "无法预览：old/new 不是字符串"
                if max(len(old.encode("utf-8")), len(new.encode("utf-8"))) > self.max_write_bytes:
                    return f"无法预览：old/new 超过上限 {self.max_write_bytes} 字节"
                if old == "":
                    if target.exists():
                        return "无法预览：创建目标已经存在"
                    after = new
                elif before.count(old) != 1:
                    return f"无法预览：old 出现 {before.count(old)} 次，必须唯一"
                else:
                    after = before.replace(old, new, 1)
            else:
                edits = call.arguments.get("edits")
                if not isinstance(edits, list):
                    return "无法预览：edits 不是数组"
                for index, edit in enumerate(edits, start=1):
                    if not isinstance(edit, dict):
                        return f"无法预览：第 {index} 个 edit 不是对象"
                    old = edit.get("old")
                    new = edit.get("new")
                    if not isinstance(old, str) or not isinstance(new, str) or not old:
                        return f"无法预览：第 {index} 个 edit 无效"
                    if (
                        max(len(old.encode("utf-8")), len(new.encode("utf-8")))
                        > self.max_write_bytes
                    ):
                        return f"无法预览：第 {index} 个 edit 超过字节上限"
                    if after.count(old) != 1:
                        return f"无法预览：第 {index} 个 old 出现 {after.count(old)} 次"
                    after = after.replace(old, new, 1)
            relative = str(target.relative_to(self.workspace.root))
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{relative}" if target.exists() else "/dev/null",
                    tofile=f"b/{relative}",
                )
            )
            preimage = self._write_fingerprint(call)
            preview = f"target-preimage-sha256: {preimage}\n" + (diff or "（文件内容没有变化）")
            return self._limit_preview(preview, max_chars)
        except (OSError, UnicodeDecodeError, WorkspaceViolation, ValueError) as exc:
            return f"无法预览：{exc}"

    @staticmethod
    def _limit_preview(value: str, max_chars: int) -> str:
        """在字符预算内保留预览前缀和截断标记，不改动原始文件或工具参数。"""
        if len(value) <= max_chars:
            return value
        marker = "\n...[预览已截断]"
        return value[: max(0, max_chars - len(marker))] + marker

    def _write_fingerprint(self, call: ToolCall) -> str | None:
        """读取待写文件的内容指纹，或返回缺失/非法/不可读标记；非写工具返回 None。"""
        if call.name not in {"write_or_replace", "apply_patch"}:
            return None
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return "invalid"
        try:
            target = self.workspace.resolve(path, allow_missing=True)
        except (OSError, WorkspaceViolation):
            return "invalid"
        if not target.exists():
            return "missing"
        if not target.is_file():
            return "not-file"
        try:
            return self._sha256_file(target)
        except OSError:
            return "unreadable"
