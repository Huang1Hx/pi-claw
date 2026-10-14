"""M5：文件后端与 SQLite 后端之间的一次性会话迁移。

流程：**先读全量并预检冲突 → 再逐个导入**。预检阶段不写任何数据；导入阶段每个会话原子完成，
因此中断只会留下“已完成”和“未开始”两种状态，重复运行幂等，源数据始终不被修改。
迁移只搬运规范化业务内容（消息、活动上下文 pins/锚/goal、用量账本、元数据），
不重算 Token、不改评分。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from persistence.repository import SessionRepository


class MigrationConflict(ValueError):
    """目标已存在同名但不同的会话，拒绝覆盖。"""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """一次工作区迁移的可复核结果。"""

    workspace: str
    total: int
    imported: int
    skipped: int


def migrate_workspace(
    source: SessionRepository,
    target: SessionRepository,
    workspace: str | Path,
) -> MigrationReport:
    """把一个工作区的全部会话从 source 迁移到 target。"""

    if not isinstance(workspace, (str, Path)):
        raise TypeError("workspace 必须是路径或字符串")
    infos = source.list_sessions(workspace)

    # 阶段一：预检目标冲突并读取全部内容；任何冲突在写入前就失败。
    existing = {info.session_id: info for info in target.list_sessions(workspace)}
    plan = []
    for info in infos:
        found = existing.get(info.session_id)
        if found is not None and found.created_at != info.created_at:
            raise MigrationConflict(f"目标已存在同名但不同的会话：{info.session_id}")
        messages = source.load_messages(info.session_id, workspace)
        context = source.load_active_context(info.session_id, workspace)
        usage = source.usage_records(info.session_id, workspace)
        plan.append((info, messages, context, usage))

    # 阶段二：逐个原子导入；重复运行对已存在会话幂等跳过。
    imported = 0
    skipped = 0
    for info, messages, context, usage in plan:
        if target.import_session(info, messages, context, usage):
            imported += 1
        else:
            skipped += 1
    return MigrationReport(
        workspace=str(workspace),
        total=len(plan),
        imported=imported,
        skipped=skipped,
    )
