"""宿主写工具提交日志：只记录成功提交的改动，供显式回滚使用。

日志只保存回滚所需的最小信息（路径、类型、preimage、版本），不进入对话上下文或 Trace 正文；
有界记录，超出上限时停止记录并标记 `incomplete`，避免内存无界增长。
"""

from __future__ import annotations

from dataclasses import dataclass

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

    @property
    def records(self) -> tuple[WriteRecord, ...]:
        """按提交顺序返回不可变记录。"""
        return tuple(self._records)

    def clear(self) -> None:
        """清空记录与 incomplete 标记。"""
        self._records.clear()
        self.incomplete = False
