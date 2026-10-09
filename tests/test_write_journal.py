"""宿主写入日志：只记录成功提交，供显式回滚使用。"""

from __future__ import annotations

import pytest

from core.journal import WriteJournal
from core.schemas import ToolCall
from security.policy import WorkspacePolicy
from security.secure_fs import SecureFileVersion
from tools import ToolExecutor


def executor(workspace, **kwargs):
    return ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True),
        approval_callback=lambda *args: True,
        **kwargs,
    )


def run(executor_, name, arguments):
    return executor_.execute(ToolCall("call", name, arguments), approved=True)


def test_journal_rejects_inconsistent_records() -> None:
    """非法记录必须 fail closed，避免回滚时用错误 preimage 覆盖文件。"""
    journal = WriteJournal()
    version = SecureFileVersion(True)
    with pytest.raises(ValueError, match="kind"):
        journal.record(
            relative_name="a.txt",
            kind="unknown",
            preimage=None,
            pre_version=version,
            post_version=version,
        )
    with pytest.raises(ValueError, match="preimage"):
        journal.record(
            relative_name="a.txt",
            kind="create",
            preimage="x",
            pre_version=version,
            post_version=version,
        )
    with pytest.raises(ValueError, match="preimage"):
        journal.record(
            relative_name="a.txt",
            kind="replace",
            preimage=None,
            pre_version=version,
            post_version=version,
        )


def test_create_is_recorded_without_preimage(workspace) -> None:
    executor_ = executor(workspace)
    run(executor_, "write_or_replace", {"path": "new.txt", "old": "", "new": "hello\n"})
    records = executor_.journal.records
    assert len(records) == 1
    assert records[0].kind == "create"
    assert records[0].preimage is None
    assert records[0].path == "new.txt"
    assert records[0].post_version.exists is True


def test_replace_and_patch_record_preimage(workspace) -> None:
    executor_ = executor(workspace)
    run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha\n", "new": "Alpha\n"},
    )
    run(
        executor_,
        "apply_patch",
        {"path": "alpha.txt", "edits": [{"old": "needle one\n", "new": "needle 1\n"}]},
    )
    records = executor_.journal.records
    assert [record.kind for record in records] == ["replace", "replace"]
    assert records[0].preimage == "alpha\nneedle one\n"
    assert records[1].preimage == "Alpha\nneedle one\n"


def test_failed_or_denied_writes_are_not_recorded(workspace) -> None:
    executor_ = executor(workspace)
    missing = run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "absent\n", "new": "x\n"},
    )
    assert not missing.ok and executor_.journal.records == ()
    denied = ToolExecutor(WorkspacePolicy(workspace, allow_write=False))
    result = denied.execute(
        ToolCall("deny", "write_or_replace", {"path": "x.txt", "old": "", "new": "x\n"})
    )
    assert not result.ok and denied.journal.records == ()


def test_journal_is_bounded_and_marks_incomplete(workspace) -> None:
    executor_ = executor(workspace, max_write_bytes=512_000)
    executor_.journal = WriteJournal(max_entries=1)
    run(executor_, "write_or_replace", {"path": "a.txt", "old": "", "new": "a\n"})
    run(executor_, "write_or_replace", {"path": "b.txt", "old": "", "new": "b\n"})
    assert len(executor_.journal.records) == 1
    assert executor_.journal.incomplete is True
