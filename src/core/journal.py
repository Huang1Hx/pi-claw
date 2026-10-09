"""宿主写工具提交日志：只记录成功提交的改动，供显式回滚使用。

日志只保存回滚所需的最小信息（路径、类型、preimage、版本），不进入对话上下文或 Trace 正文；
有界记录，超出上限时停止记录并标记 `incomplete`，避免内存无界增长。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from security.secure_fs import SecureFileVersion

CREATE = "create"
REPLACE = "replace"
KINDS = (CREATE, REPLACE)


@dataclass(frozen=True, slots=True)
class WriteRecord:
    """一次成功提交的可回滚记录。"""

    path: str
    kind: str
    preimage: str | None
    pre_version: SecureFileVersion
    post_version: SecureFileVersion

    def to_dict(self) -> dict:
        """输出不含正文的审计摘要。"""
        return {
            "path": self.path,
            "kind": self.kind,
            "had_preimage": self.preimage is not None,
            "preimage_bytes": len(self.preimage.encode("utf-8")) if self.preimage else 0,
        }


class WriteJournal:
    """有界记录宿主写工具的提交，顺序即真实提交顺序，供逆序回滚。"""

    def __init__(self, *, max_entries: int = 64, max_bytes: int = 512_000) -> None:
        """校验边界；不满足时 fail closed，避免记录被静默截断而误以为可完整回滚。"""
        if type(max_entries) is not int or not 1 <= max_entries <= 1024:
            raise ValueError("max_entries 必须是 1–1024 的整数")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes 必须为正整数")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._records: list[WriteRecord] = []
        self.incomplete = False

    def record(
        self,
        *,
        relative_name: str,
        kind: str,
        preimage: str | None,
        pre_version: SecureFileVersion,
        post_version: SecureFileVersion,
    ) -> bool:
        """记录一次成功提交；超出边界时停止记录并标记 incomplete，返回是否已记录。"""
        if kind not in KINDS:
            raise ValueError(f"未知 kind：{kind!r}")
        if not isinstance(relative_name, str) or not relative_name:
            raise ValueError("relative_name 必须是非空字符串")
        if kind == CREATE:
            if preimage is not None:
                raise ValueError("create 记录不得携带 preimage")
        elif not isinstance(preimage, str):
            raise ValueError("replace 记录必须携带字符串 preimage")
        if not isinstance(pre_version, SecureFileVersion) or not isinstance(
            post_version, SecureFileVersion
        ):
            raise TypeError("pre_version/post_version 必须是 SecureFileVersion")
        if len(self._records) >= self.max_entries:
            self.incomplete = True
            return False
        if preimage is not None and len(preimage.encode("utf-8")) > self.max_bytes:
            self.incomplete = True
            return False
        self._records.append(WriteRecord(relative_name, kind, preimage, pre_version, post_version))
        return True

    def rollback(self, secure_fs, *, max_bytes: int) -> RollbackSummary:
        """逆序回放已记录的提交，逐路径跟踪期望版本；不一致时跳过并上报，绝不覆盖外部改动。

        同一路径可能被多次提交（例如先创建再替换）。回滚后续提交会生成新 inode，
        因此不能拿更早记录的 post_version 直接比对；这里在每次成功回放后重读该路径版本，
        作为下一条更早记录必须先满足的期望版本。对外部改动仍然 fail closed。
        """
        outcomes: list[RollbackOutcome] = []
        restored = deleted = skipped = failed = 0
        expected: dict[str, SecureFileVersion] = {}
        blocked: set[str] = set()
        for record in reversed(self._records):
            path = Path(record.path)
            if record.path in blocked:
                skipped += 1
                outcomes.append(RollbackOutcome(record.path, "skipped", "先前回滚失败，跳过"))
                continue
            exp = expected.get(record.path, record.post_version)
            try:
                current = secure_fs.version(path, allow_missing=True, max_bytes=max_bytes)
            except Exception as exc:  # noqa: BLE001 - 逐条容错，绝不伪装成功
                failed += 1
                blocked.add(record.path)
                outcomes.append(RollbackOutcome(record.path, "failed", _reason(exc)))
                continue
            if not current.exists:
                skipped += 1
                outcomes.append(RollbackOutcome(record.path, "skipped", "文件已不存在"))
                continue
            if current != exp:
                skipped += 1
                outcomes.append(RollbackOutcome(record.path, "skipped", "提交后已被外部改动"))
                continue
            try:
                if record.kind == CREATE:
                    secure_fs.atomic_delete_file(path, expected=exp, max_bytes=max_bytes)
                    deleted += 1
                    expected.pop(record.path, None)
                    outcomes.append(RollbackOutcome(record.path, "deleted"))
                else:
                    assert record.preimage is not None
                    secure_fs.atomic_replace_text(
                        path,
                        record.preimage,
                        expected=exp,
                        max_bytes=max_bytes,
                    )
                    restored += 1
                    expected[record.path] = secure_fs.version(path, max_bytes=max_bytes)
                    outcomes.append(RollbackOutcome(record.path, "restored"))
            except Exception as exc:  # noqa: BLE001 - 逐条失败如实计入 failed
                failed += 1
                blocked.add(record.path)
                outcomes.append(RollbackOutcome(record.path, "failed", _reason(exc)))
        return RollbackSummary(restored, deleted, skipped, failed, self.incomplete, tuple(outcomes))

    @property
    def records(self) -> tuple[WriteRecord, ...]:
        """按提交顺序返回不可变记录。"""
        return tuple(self._records)

    def clear(self) -> None:
        """清空记录与 incomplete 标记。"""
        self._records.clear()
        self.incomplete = False


@dataclass(frozen=True)
class RollbackOutcome:
    """单条记录的回滚结果；reason 只含稳定标记，不含文件正文。"""

    path: str
    action: str  # restored | deleted | skipped | failed
    reason: str | None = None

    def to_dict(self) -> dict:
        """输出可审计摘要，不携带正文。"""
        payload = {"path": self.path, "action": self.action}
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class RollbackSummary:
    """回滚整体摘要：逐条计数与可审计明细，incomplete 表示日志曾有截断。"""

    restored: int
    deleted: int
    skipped: int
    failed: int
    incomplete: bool
    outcomes: tuple[RollbackOutcome, ...] = ()

    def to_dict(self) -> dict:
        """输出不含正文的摘要，供 RunResult 与 Trace 使用。"""
        return {
            "restored": self.restored,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "incomplete": self.incomplete,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def _reason(exc: BaseException) -> str:
    """回滚失败原因只保留稳定摘要，避免把正文或路径细节放大。"""
    return str(exc)[:200] or exc.__class__.__name__
