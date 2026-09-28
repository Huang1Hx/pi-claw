"""工作区路径解析与敏感文件边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WorkspaceViolation(ValueError):
    """路径试图越过工作区或访问敏感文件。"""


class Workspace:
    """把所有文件访问约束到一个解析后的真实目录内。"""

    _SENSITIVE_PARTS = {
        ".git",
        ".env",
        ".aws",
        ".ssh",
        "credentials",
        "credential",
        "secrets",
        "secret",
        "id_rsa",
        "id_ed25519",
    }
    _SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}

    def __init__(self, root: str | Path) -> None:
        """要求工作区已存在且为目录，并固定其真实路径作为后续访问边界。"""
        candidate = Path(root).expanduser()
        if not candidate.exists():
            raise ValueError(f"工作区不存在：{candidate}")
        if not candidate.is_dir():
            raise ValueError(f"工作区不是目录：{candidate}")
        self.root = candidate.resolve(strict=True)

    def resolve(self, path: str | Path = ".", *, allow_missing: bool = False) -> Path:
        """解析路径，并拒绝显式穿越、符号链接越界和敏感路径。"""

        raw = Path(path)
        if ".." in raw.parts:
            raise WorkspaceViolation("路径包含不允许的 '..' 穿越片段")

        candidate = raw if raw.is_absolute() else self.root / raw
        # strict=False 仍会解析已存在父目录中的符号链接，因此可阻止“新文件名 + 越界
        # symlink 父目录”的写入方式。
        resolved = candidate.resolve(strict=not allow_missing)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation("路径位于工作区之外") from exc

        lexical_parts = tuple(part.lower() for part in raw.parts)
        resolved_parts = tuple(part.lower() for part in relative.parts)
        self._check_sensitive((*lexical_parts, *resolved_parts))
        return resolved

    @classmethod
    def _check_sensitive(cls, parts: tuple[str, ...]) -> None:
        """检查原始与解析后路径片段，拒绝凭据目录、.env 变体和密钥扩展名。"""
        for part in parts:
            stem = Path(part).stem
            if (
                part in cls._SENSITIVE_PARTS
                or stem in cls._SENSITIVE_PARTS
                or part.startswith(".env.")
            ):
                raise WorkspaceViolation(f"拒绝访问敏感路径片段：{part}")
            if Path(part).suffix.lower() in cls._SENSITIVE_SUFFIXES:
                raise WorkspaceViolation(f"拒绝访问凭据/密钥文件：{part}")

    def relative(self, path: str | Path) -> str:
        """返回工作区内经过验证的相对路径。"""
        return str(self.resolve(path).relative_to(self.root))


def ensure_json_object(value: Any, *, name: str = "arguments") -> dict[str, Any]:
    """确保工具参数为 JSON 对象。"""
    if not isinstance(value, dict):
        raise TypeError(f"{name} 必须是对象")
    return value


# 为直观导入保留别名；实际权限决策实现在 policy.py，避免 Workspace 操作模型。
def __getattr__(name: str) -> Any:
    """按需提供 WorkspacePolicy 兼容导出，避免模块加载时产生循环依赖。"""
    if name == "WorkspacePolicy":
        from security.policy import WorkspacePolicy

        return WorkspacePolicy
    raise AttributeError(name)
