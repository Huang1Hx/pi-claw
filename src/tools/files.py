"""文件工具共享的安全相对路径、版本指纹和有界读取辅助。"""

from __future__ import annotations

from pathlib import Path

from core.schemas import ToolCall
from security.secure_fs import SecureFileSnapshot, SecureFileVersion


class FileStorage:
    """由 ToolExecutor 组合，复用工作区和资源预算。"""

    def _relative_path(self, path: str | Path) -> Path:
        """只做词法归一化；真实节点访问交由 SecureWorkspaceFS 逐段验证。"""
        return self.workspace.lexical_relative(path)

    def _read_full(self, relative: Path) -> SecureFileSnapshot:
        """按写入处理预算完整读取并返回绑定 inode/type/preimage 的快照。"""
        return self.workspace.secure_fs.read_full(relative, max_bytes=self.max_write_bytes)

    def _require_approved_version(self, actual: SecureFileVersion) -> None:
        """handler 首次读取必须仍是用户看到并批准的同一文件版本。"""
        approved = self._approved_file_version
        if approved is not None and actual != approved:
            raise ValueError("目标文件不再匹配审批预览版本，拒绝执行")

    def _record_write(
        self,
        relative: Path,
        *,
        kind: str,
        preimage: str | None,
        pre_version: SecureFileVersion,
    ) -> None:
        """在成功提交后把改动写入宿主日志；未配置日志时静默跳过。"""
        journal = getattr(self, "journal", None)
        if journal is None:
            return
        post_version = self.workspace.secure_fs.version(relative, max_bytes=self.max_write_bytes)
        journal.record(
            relative_name=relative.as_posix(),
            kind=kind,
            preimage=preimage,
            pre_version=pre_version,
            post_version=post_version,
        )

    def _write_version(self, call: ToolCall) -> SecureFileVersion | str | None:
        """返回审批绑定版本；异常使用稳定标记，不能把宿主路径写进预览。"""
        if call.name not in {"write_or_replace", "apply_patch"}:
            return None
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return "invalid"
        try:
            relative = self._relative_path(path)
            return self.workspace.secure_fs.version(
                relative,
                allow_missing=True,
                max_bytes=self.max_write_bytes,
            )
        except (OSError, ValueError):
            return "invalid"
