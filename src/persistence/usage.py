"""会话级用量账本：按 run_id 幂等保存已收到的服务端 Token 计数。"""

import json
from pathlib import Path
from typing import Any

from persistence.models import _SESSION_ID_PATTERN


class UsageMixin:
    """沿用 SessionStore 的文件锁；失败请求是否计费由供应商决定。"""

    def record_usage(
        self, session_id: str, workspace: str | Path, run_id: str, usage: dict[str, Any]
    ) -> None:
        """同一个 run_id 只记录一次，避免重试保存造成重复计数。"""
        session_dir = self._session_dir(session_id, self._workspace_root(workspace))
        self.load(session_id, workspace)
        with self._session_write_lock(session_dir):
            records = self._read_usage(session_dir)
            # 查重和写入共用会话锁，避免两个保存者把同一回合重复计入累计 Token。
            if run_id in records:
                return
            fields = (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "requests",
                "requests_with_usage",
                "requests_without_usage",
                "retries",
            )
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in fields):
                raise ValueError("无效用量计数")
            records[run_id] = {k: usage[k] for k in fields}
            self._sum_usage(records)
            encoded = json.dumps(records, ensure_ascii=False).encode()
            if len(records) > self.max_events or len(encoded) > self.max_bytes:
                raise ValueError("会话用量账本超过上限")
            self._atomic_write(session_dir / "usage.json", encoded)

    def total_usage(self, session_id: str, workspace: str | Path) -> dict[str, int]:
        """恢复会话时重新聚合，不依赖本进程内存。"""
        session_dir = self._session_dir(session_id, self._workspace_root(workspace))
        self.load(session_id, workspace)
        with self._session_write_lock(session_dir):
            records = self._read_usage(session_dir)
        return self._sum_usage(records)

    @staticmethod
    def _sum_usage(records: dict[str, Any]) -> dict[str, int]:
        """聚合一个完整账本；拒绝损坏的回合，避免部分字段被当作零或静默少计。"""
        totals: dict[str, int] = {}
        fields = {
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "requests",
            "requests_with_usage",
            "requests_without_usage",
            "retries",
        }
        for usage in records.values():
            if set(usage) != fields:
                raise ValueError("会话用量账本字段不完整")
            for key, value in usage.items():
                if type(value) is not int or value < 0:
                    raise ValueError("会话用量账本含无效计数")
                totals[key] = totals.get(key, 0) + value
            if (
                usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]
                or usage["requests"]
                != usage["requests_with_usage"] + usage["requests_without_usage"]
            ):
                raise ValueError("会话用量账本计数不一致")
        return totals

    def workspace_usage(self, workspace: str | Path) -> dict[str, Any]:
        """只聚合这个真实工作区的会话，不递归其他项目；坏账本显式计入缺口。

        每个会话独立持锁读取，逐个释放后再读下一会话；不是跨会话的全局原子快照。
        用量仍按原 run_id 账本幂等保存；这里只读，不迁移或重算历史消息 Token。
        """
        root = self._workspace_root(workspace)
        group = self._workspace_group(root)
        self._reject_symlink(group)
        report = {
            "totals": {},
            "session_count": 0,
            "recorded_sessions": 0,
            "unrecorded_sessions": 0,
            "unreadable_sessions": 0,
        }
        if not group.exists():
            return report
        for child in group.iterdir():
            if not _SESSION_ID_PATTERN.fullmatch(child.name):
                continue
            if not child.is_dir() and not child.is_symlink():
                continue
            report["session_count"] += 1
            try:
                totals = self.total_usage(child.name, root)
            except (OSError, ValueError, TypeError, KeyError):
                report["unreadable_sessions"] += 1
                continue
            if not totals:
                report["unrecorded_sessions"] += 1
                continue
            report["recorded_sessions"] += 1
            for key, value in totals.items():
                report["totals"][key] = report["totals"].get(key, 0) + value
        return report

    def _read_usage(self, session_dir: Path) -> dict[str, Any]:
        """有界读取且拒绝符号链接；旧会话没有 usage 文件时返回空账本。"""
        path = session_dir / "usage.json"
        self._reject_symlink(path)
        if not path.exists():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("会话用量账本超过读取上限")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or len(value) > self.max_events
            or not all(isinstance(item, dict) for item in value.values())
        ):
            raise ValueError("会话用量账本格式无效")
        return value
