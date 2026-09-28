"""选择默认示例工作区，不改变启动目录的环境变量与审计配置来源。"""

from pathlib import Path


def resolve_workspace(
    explicit: Path | None, *, cwd: Path | None = None, create: bool = True
) -> Path:
    """显式目录优先，否则使用启动目录/projects；默认目录禁止符号链接替换。

    create=False 供只读 sessions 命令使用，不为查询创建目录。不会迁移旧会话或清空已有项目。
    显式工作区必须已存在，避免路径拼写错误时悄悄建立新目录。
    """
    if explicit is not None:
        path = explicit.expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("--workspace 必须指向已有目录")
        return path
    base = (cwd or Path.cwd()).resolve(strict=True)
    path = base / "projects"
    if path.is_symlink():
        raise ValueError("默认 projects 工作区不能是符号链接；请显式指定可信 --workspace")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.exists() and not path.is_dir():
        raise ValueError("默认 projects 工作区不是目录")
    resolved = path.resolve(strict=False)
    if resolved != path:
        raise ValueError("默认工作区路径发生变化，拒绝继续")
    return resolved
