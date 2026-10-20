"""审批前 diff 预览与文件版本指纹。"""

from __future__ import annotations

import difflib
import json

from core.schemas import ToolCall
from security.secure_fs import SecureFileVersion
from security.workspace import WorkspaceViolation


class PreviewTools:
    """由 ToolExecutor 绑定工作区和资源预算后使用。"""

    def preview(self, call: ToolCall, *, max_chars: int = 12_000) -> str | None:
        """为副作用调用生成有界预览；预览失败不会绕过后续真实校验。"""

        value, _version = self._preview_with_version(call, max_chars=max_chars)
        return value

    def _preview_with_version(
        self,
        call: ToolCall,
        *,
        max_chars: int = 12_000,
    ) -> tuple[str | None, SecureFileVersion | str | None]:
        """用同一稳定快照生成 diff 和 SHA，并把该版本交给审批后复核。"""

        if max_chars <= 0:
            raise ValueError("preview max_chars 必须大于 0")
        try:
            if call.name == "run_command":
                argv = call.arguments.get("argv", [])
                cwd = call.arguments.get("cwd", ".")
                return (
                    self._limit_preview(
                        f"cwd: {cwd}\nargv: {json.dumps(argv, ensure_ascii=False)}",
                        max_chars,
                    ),
                    None,
                )
            if call.name not in {"write_or_replace", "apply_patch"}:
                return None, None
            path = call.arguments.get("path")
            if not isinstance(path, str):
                return "无法预览：path 不是字符串", "invalid"
            relative = self._relative_path(path)
            if call.name == "write_or_replace":
                old = call.arguments.get("old")
                new = call.arguments.get("new")
                if not isinstance(old, str) or not isinstance(new, str):
                    return "无法预览：old/new 不是字符串", "invalid"
                if len(old.encode("utf-8")) > self.max_write_bytes:
                    return f"无法预览：old 超过写入处理上限 {self.max_write_bytes} 字节", "invalid"
                if len(new.encode("utf-8")) > self.max_write_bytes:
                    return f"无法预览：new 超过写入上限 {self.max_write_bytes} 字节", "invalid"
            else:
                edits = call.arguments.get("edits")
                if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
                    return "无法预览：edits 必须包含 1 到 50 个精确替换", "invalid"
                for index, edit in enumerate(edits, start=1):
                    if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
                        return (
                            f"无法预览：第 {index} 个 edit 必须且只能包含 old、new",
                            "invalid",
                        )
                    old = edit.get("old")
                    new = edit.get("new")
                    if not isinstance(old, str) or not old or not isinstance(new, str):
                        return f"无法预览：第 {index} 个 edit 无效", "invalid"
                    if max(len(old.encode("utf-8")), len(new.encode("utf-8"))) > (
                        self.max_write_bytes
                    ):
                        return f"无法预览：第 {index} 个 edit 超过字节上限", "invalid"
            snapshot = self.workspace.secure_fs.read_optional_full(
                relative,
                max_bytes=self.max_write_bytes,
            )
            version = snapshot.version
            before = snapshot.content.decode("utf-8") if version.exists else ""
            after = before
            if call.name == "write_or_replace":
                assert isinstance(old, str) and isinstance(new, str)
                if old == "":
                    if version.exists:
                        return "无法预览：创建目标已经存在", "invalid"
                    after = new
                elif before.count(old) != 1:
                    count = before.count(old)
                    detail = "文本不存在" if count == 0 else "必须唯一"
                    return f"无法预览：old 出现 {count} 次，{detail}", "invalid"
                else:
                    after = before.replace(old, new, 1)
                    if len(after.encode("utf-8")) > self.max_write_bytes:
                        return (
                            f"无法预览：替换结果超过写入上限 {self.max_write_bytes} 字节",
                            "invalid",
                        )
            else:
                assert isinstance(edits, list)
                for index, edit in enumerate(edits, start=1):
                    assert isinstance(edit, dict)
                    old = edit.get("old")
                    new = edit.get("new")
                    assert isinstance(old, str) and old and isinstance(new, str)
                    count = after.count(old)
                    if count != 1:
                        return (
                            f"无法预览：第 {index} 个 edit 的 old 出现 {count} 次，必须唯一",
                            "invalid",
                        )
                    after = after.replace(old, new, 1)
                    if len(after.encode("utf-8")) > self.max_write_bytes:
                        return (
                            f"无法预览：第 {index} 个 edit 的补丁结果超过写入上限 "
                            f"{self.max_write_bytes} 字节",
                            "invalid",
                        )
            display = relative.as_posix()
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{display}" if version.exists else "/dev/null",
                    tofile=f"b/{display}",
                )
            )
            preimage = version.sha256 if version.exists else "missing"
            preview = f"target-preimage-sha256: {preimage}\n" + (diff or "（文件内容没有变化）")
            return self._limit_preview(preview, max_chars), version
        except (OSError, UnicodeDecodeError, WorkspaceViolation, ValueError) as exc:
            return f"无法预览：{exc}", "invalid"

    @staticmethod
    def _limit_preview(value: str, max_chars: int) -> str:
        """在字符预算内保留预览前缀和截断标记，不改动原始文件或工具参数。"""
        if len(value) <= max_chars:
            return value
        marker = "\n...[预览已截断]"
        return value[: max(0, max_chars - len(marker))] + marker
