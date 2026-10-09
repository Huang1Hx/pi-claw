"""显式回滚：版本校验删除、逆序恢复与失败/跳过如实上报。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent import Agent
from core.schemas import ModelReply, ToolCall
from persistence.trace import TraceReplay, TraceWriter
from providers import ScriptedProvider
from security.policy import WorkspacePolicy
from security.secure_fs import SecureFileViolation
from tools import ToolExecutor


def executor(workspace: Path) -> ToolExecutor:
    return ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True),
        approval_callback=lambda *args: True,
    )


def run(executor_: ToolExecutor, name: str, arguments: dict) -> None:
    result = executor_.execute(ToolCall("rollback", name, arguments))
    assert result.ok, result.error


def rollback(executor_: ToolExecutor):
    return executor_.journal.rollback(
        executor_.workspace.secure_fs, max_bytes=executor_.max_write_bytes
    )


def test_created_file_is_deleted(workspace: Path) -> None:
    """回滚本次创建的文件应删除，且摘要如实计数。"""
    executor_ = executor(workspace)
    run(executor_, "write_or_replace", {"path": "new.txt", "old": "", "new": "hello\n"})
    assert (workspace / "new.txt").exists()

    summary = rollback(executor_)

    assert summary.deleted == 1 and summary.restored == 0 and summary.failed == 0
    assert not (workspace / "new.txt").exists()


def test_replaced_file_restores_preimage(workspace: Path) -> None:
    """回滚替换应写回原始正文。"""
    executor_ = executor(workspace)
    run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha\nneedle one\n", "new": "changed\n"},
    )

    summary = rollback(executor_)

    assert summary.restored == 1 and summary.deleted == 0
    assert (workspace / "alpha.txt").read_text(encoding="utf-8") == "alpha\nneedle one\n"


def test_rollback_replays_in_reverse_order(workspace: Path) -> None:
    """先创建后替换：逆序回滚先恢复再删除，最终文件不存在。"""
    executor_ = executor(workspace)
    run(executor_, "write_or_replace", {"path": "combo.txt", "old": "", "new": "v1\n"})
    run(
        executor_,
        "write_or_replace",
        {"path": "combo.txt", "old": "v1\n", "new": "v2\n"},
    )

    summary = rollback(executor_)

    assert summary.restored == 1 and summary.deleted == 1
    assert not (workspace / "combo.txt").exists()


def test_externally_changed_file_is_skipped(workspace: Path) -> None:
    """提交后文件被外部改动时必须跳过，绝不覆盖。"""
    executor_ = executor(workspace)
    run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha\nneedle one\n", "new": "agent\n"},
    )
    (workspace / "alpha.txt").write_text("external\n", encoding="utf-8")

    summary = rollback(executor_)

    assert summary.skipped == 1 and summary.restored == 0 and summary.failed == 0
    assert (workspace / "alpha.txt").read_text(encoding="utf-8") == "external\n"


def test_rollback_failure_is_reported(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回滚底层出错不得伪装成功，必须计入 failed 且保留现场。"""
    executor_ = executor(workspace)
    run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha\nneedle one\n", "new": "agent\n"},
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise SecureFileViolation("磁盘只读")

    monkeypatch.setattr(executor_.workspace.secure_fs, "atomic_replace_text", boom)

    summary = rollback(executor_)

    assert summary.failed == 1 and summary.restored == 0
    assert (workspace / "alpha.txt").read_text(encoding="utf-8") == "agent\n"


def test_atomic_delete_rejects_version_mismatch(workspace: Path) -> None:
    """删除必须绑定记录版本；文件已被改动则拒绝删除。"""
    executor_ = executor(workspace)
    run(executor_, "write_or_replace", {"path": "new.txt", "old": "", "new": "hello\n"})
    record = executor_.journal.records[0]
    (workspace / "new.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SecureFileViolation):
        executor_.workspace.secure_fs.atomic_delete_file(
            Path("new.txt"),
            expected=record.post_version,
            max_bytes=executor_.max_write_bytes,
        )
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "tampered\n"


def test_agent_rollback_attaches_summary_and_traces(workspace: Path, tmp_path: Path) -> None:
    """Agent 显式回滚应恢复文件、附回摘要并写入回滚 Trace 事件。"""
    trace_path = tmp_path / "rollback.jsonl"
    provider = ScriptedProvider(
        [
            ModelReply.call(
                ToolCall(
                    "w1",
                    "write_or_replace",
                    {"path": "alpha.txt", "old": "alpha\nneedle one\n", "new": "changed\n"},
                )
            ),
            ModelReply.final("已完成"),
        ]
    )
    agent = Agent(
        provider,
        executor(workspace),
        trace=TraceWriter(trace_path),
    )
    result = agent.run("修改 alpha.txt")
    assert result.status.value == "completed"

    rolled = agent.rollback_run(result)

    assert rolled.rollback is not None
    assert rolled.rollback["restored"] == 1
    assert (workspace / "alpha.txt").read_text(encoding="utf-8") == "alpha\nneedle one\n"
    events = [event["event"] for event in TraceReplay(trace_path).read()]
    assert "rollback_started" in events
    assert "rollback_restored" in events
    assert "rollback_completed" in events


def test_multiple_replaces_to_same_path_replay_in_reverse(workspace: Path) -> None:
    """同一路径多次替换也必须逐条逆序还原，不能因 inode 变化而误判为外部改动。"""
    executor_ = executor(workspace)
    run(
        executor_,
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha\nneedle one\n", "new": "v1\n"},
    )
    run(executor_, "write_or_replace", {"path": "alpha.txt", "old": "v1\n", "new": "v2\n"})

    summary = rollback(executor_)

    assert summary.restored == 2 and summary.skipped == 0 and summary.failed == 0
    assert (workspace / "alpha.txt").read_text(encoding="utf-8") == "alpha\nneedle one\n"


def test_last_only_undo_consumes_one_record(workspace: Path) -> None:
    """`/undo` 每次只撤销最近一条写入，并消费已处理记录。"""
    executor_ = executor(workspace)
    run(executor_, "write_or_replace", {"path": "a.txt", "old": "", "new": "a\n"})
    run(executor_, "write_or_replace", {"path": "b.txt", "old": "", "new": "b\n"})

    first = executor_.journal.rollback(
        executor_.workspace.secure_fs, max_bytes=executor_.max_write_bytes, last_only=True
    )
    assert first.deleted == 1
    assert not (workspace / "b.txt").exists() and (workspace / "a.txt").exists()

    second = executor_.journal.rollback(
        executor_.workspace.secure_fs, max_bytes=executor_.max_write_bytes, last_only=True
    )
    assert second.deleted == 1
    assert not (workspace / "a.txt").exists()

    empty = executor_.journal.rollback(
        executor_.workspace.secure_fs, max_bytes=executor_.max_write_bytes, last_only=True
    )
    assert empty.deleted == 0 and empty.restored == 0 and empty.skipped == 0
    assert executor_.journal.records == ()
