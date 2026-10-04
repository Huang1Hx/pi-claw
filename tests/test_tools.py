from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from core.control import ExecutionCancelled, ExecutionControl
from core.schemas import ToolCall
from security.approval import TerminalApprovalBroker
from security.policy import WorkspacePolicy
from tools import ToolExecutor, ToolRegistry


def execute(executor: ToolExecutor, name: str, **arguments: object):
    return executor.execute(ToolCall("call-1", name, arguments))


def test_registry_rejects_empty_and_duplicate_names() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="不能为空"):
        registry.register("", lambda: None)
    registry.register("demo", lambda: "ok")
    with pytest.raises(ValueError, match="已注册"):
        registry.register("demo", lambda: "duplicate")


def test_builtin_registry_exposes_structured_schemas(workspace: Path) -> None:
    schemas = ToolExecutor(WorkspacePolicy(workspace)).registry.openai_schemas()
    names = {item["function"]["name"] for item in schemas}
    assert names == {
        "list_files",
        "search_text",
        "read_file",
        "write_or_replace",
        "apply_patch",
        "run_command",
    }
    assert all(item["type"] == "function" for item in schemas)


def test_read_only_tools_do_not_change_files(workspace: Path) -> None:
    executor = ToolExecutor(WorkspacePolicy(workspace))
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}

    listed = execute(executor, "list_files", path=".")
    searched = execute(executor, "search_text", query="needle", path=".")
    read = execute(executor, "read_file", path="alpha.txt")

    after = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    assert listed.ok and "alpha.txt" in listed.output["files"]
    assert searched.ok and len(searched.output["matches"]) == 2
    assert read.ok and read.output["content"].startswith("alpha")
    assert after == before


def test_write_requires_approval(workspace: Path) -> None:
    executor = ToolExecutor(WorkspacePolicy(workspace, allow_write=True))
    result = execute(
        executor,
        "write_or_replace",
        path="alpha.txt",
        old="alpha",
        new="changed",
    )
    assert not result.ok
    assert "未获批准" in result.error
    assert (workspace / "alpha.txt").read_text(encoding="utf-8").startswith("alpha")


def test_approval_callback_can_return_specific_rejection_reason(workspace: Path) -> None:
    """宿主自动审批器可向 Agent 返回可操作原因，布尔回调继续保持兼容。"""
    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True),
        approval_callback=lambda *_: "候选代码包含不允许的语法节点：While",
    )
    result = execute(
        executor,
        "write_or_replace",
        path="alpha.txt",
        old="alpha",
        new="changed",
    )
    assert result.ok is False
    assert result.error == "未获批准：候选代码包含不允许的语法节点：While"


def test_exact_replace_after_explicit_approval(workspace: Path) -> None:
    seen: list[str] = []

    def approve(tool_call: ToolCall, _decision: object) -> bool:
        seen.append(tool_call.name)
        return True

    executor = ToolExecutor(WorkspacePolicy(workspace, allow_write=True), approval_callback=approve)
    result = execute(
        executor,
        "write_or_replace",
        path="alpha.txt",
        old="alpha",
        new="changed",
    )
    assert result.ok
    assert result.output["replacements"] == 1
    assert seen == ["write_or_replace"]
    assert (workspace / "alpha.txt").read_text(encoding="utf-8").startswith("changed")


def test_cancellation_during_approval_prevents_handler_side_effect(workspace: Path) -> None:
    """批准返回后必须重新检查生命周期，取消的写入不能进入真实 handler。"""
    control = ExecutionControl()

    def cancel_then_approve(*_args) -> bool:
        control.cancel()
        return True

    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True), approval_callback=cancel_then_approve
    )
    call = ToolCall(
        "write",
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha", "new": "changed"},
    )

    with pytest.raises(ExecutionCancelled):
        executor.execute(call, control=control)
    assert (workspace / "alpha.txt").read_text(encoding="utf-8").startswith("alpha")


@pytest.mark.parametrize(
    ("content", "old", "message"),
    [
        ("alpha\n", "missing", "不存在"),
        ("same same\n", "same", "必须唯一"),
    ],
)
def test_exact_replace_rejects_non_unique_match_without_modification(
    tmp_path: Path, content: str, old: str, message: str
) -> None:
    target = tmp_path / "target.txt"
    target.write_text(content, encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True), approval_callback=lambda *_: True
    )
    result = execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old=old,
        new="replacement",
    )
    assert not result.ok
    assert message in result.error
    assert target.read_text(encoding="utf-8") == content


def test_create_is_exclusive(workspace: Path) -> None:
    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True), approval_callback=lambda *_: True
    )
    created = execute(
        executor,
        "write_or_replace",
        path="created.txt",
        old="",
        new="new",
    )
    duplicate = execute(
        executor,
        "write_or_replace",
        path="created.txt",
        old="",
        new="overwritten",
    )
    assert created.ok and created.output["created"] is True
    assert not duplicate.ok
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "new"


def test_apply_patch_applies_multiple_exact_edits_after_approval(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True), approval_callback=lambda *_: True
    )
    result = execute(
        executor,
        "apply_patch",
        path="target.txt",
        edits=[
            {"old": "first", "new": "FIRST"},
            {"old": "second", "new": "SECOND"},
        ],
    )
    assert result.ok
    assert result.output["edits_applied"] == 2
    assert target.read_text(encoding="utf-8") == "FIRST\nSECOND\n"


def test_apply_patch_is_atomic_when_later_edit_is_not_unique(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    original = "first\nrepeated\nrepeated\n"
    target.write_text(original, encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True), approval_callback=lambda *_: True
    )
    result = execute(
        executor,
        "apply_patch",
        path="target.txt",
        edits=[
            {"old": "first", "new": "FIRST"},
            {"old": "repeated", "new": "REPLACED"},
        ],
    )
    assert not result.ok
    assert "第 2 个 edit" in result.error
    assert "必须唯一" in result.error
    assert target.read_text(encoding="utf-8") == original


def test_write_approval_receives_bounded_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    previews: list[str] = []

    def approve(_call: ToolCall, _decision: object, preview: str | None) -> bool:
        previews.append(preview or "")
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=approve,
    )
    result = execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert result.ok
    assert "--- a/target.txt" in previews[0]
    assert "+++ b/target.txt" in previews[0]
    assert "-before" in previews[0]
    assert "+after" in previews[0]


def test_write_refuses_when_target_changes_during_approval(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")

    def mutate_then_approve(_call: ToolCall, _decision: object, _preview: str | None) -> bool:
        target.write_text("external change\n", encoding="utf-8")
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=mutate_then_approve,
    )
    result = execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert not result.ok
    assert "审批后目标文件发生变化" in result.error
    assert target.read_text(encoding="utf-8") == "external change\n"


def test_session_write_approval_is_bound_to_reviewed_file_version(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("a\n", encoding="utf-8")
    answers = iter(["a", "n"])
    broker = TerminalApprovalBroker(
        input_fn=lambda _prompt: next(answers),
        interactive=True,
    )
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=broker,
    )
    call = ToolCall(
        "write-versioned",
        "write_or_replace",
        {"path": "target.txt", "old": "a", "new": "b"},
    )

    assert executor.execute(call).ok
    target.write_text("external change\na\n", encoding="utf-8")
    refused = executor.execute(call)

    assert not refused.ok
    assert "未获批准" in str(refused.error)
    assert target.read_text(encoding="utf-8") == "external change\na\n"


def test_atomic_replace_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")

    def fail_replace(*_args: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("tools.files.os.replace", fail_replace)
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_: True,
    )

    result = execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert not result.ok
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not list(tmp_path.glob(".target.txt-*.tmp"))


def test_atomic_replace_preserves_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o750)
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_: True,
    )

    result = execute(
        executor,
        "write_or_replace",
        path="script.sh",
        old="before",
        new="after",
    )

    assert result.ok
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_write_size_limits_cover_input_target_and_result(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("abcde", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_: True,
        max_write_bytes=4,
    )

    old_too_large = execute(executor, "write_or_replace", path="target.txt", old="abcde", new="x")
    new_too_large = execute(executor, "write_or_replace", path="target.txt", old="a", new="12345")
    target_too_large = execute(executor, "write_or_replace", path="target.txt", old="a", new="x")

    assert "old 超过" in old_too_large.error
    assert "new 超过" in new_too_large.error
    assert "目标文件超过" in target_too_large.error

    target.write_text("ab", encoding="utf-8")
    result_too_large = execute(executor, "write_or_replace", path="target.txt", old="a", new="xxxx")
    assert "替换结果超过" in result_too_large.error
    assert target.read_text(encoding="utf-8") == "ab"


def test_list_files_truncates_at_limit(workspace: Path) -> None:
    executor = ToolExecutor(WorkspacePolicy(workspace), max_files=2)
    result = execute(executor, "list_files", path=".")
    assert result.ok
    assert len(result.output["files"]) == 2
    assert result.output["truncated"] is True
    assert "上限 2" in result.output["reason"]


def test_search_text_truncates_matches(workspace: Path) -> None:
    executor = ToolExecutor(WorkspacePolicy(workspace), max_matches=1)
    result = execute(executor, "search_text", query="needle", path=".")
    assert result.ok
    assert len(result.output["matches"]) == 1
    assert result.output["truncated"] is True
    assert "匹配数达到上限 1" in result.output["reason"]


def test_search_text_reports_scan_budget_and_large_file(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("x" * 20, encoding="utf-8")
    (tmp_path / "small.txt").write_text("needle", encoding="utf-8")
    executor = ToolExecutor(WorkspacePolicy(tmp_path), max_scan_file_bytes=10, max_scan_bytes=4)
    result = execute(executor, "search_text", query="needle", path=".")
    assert result.ok
    assert result.output["skipped_large_files"] == 1
    assert result.output["truncated"] is True
    assert "总扫描预算" in result.output["reason"]


def test_read_file_truncates_bytes(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("abcdefgh", encoding="utf-8")
    executor = ToolExecutor(WorkspacePolicy(tmp_path), max_read_bytes=4)
    result = execute(executor, "read_file", path="long.txt")
    assert result.ok
    assert result.output["content"] == "abcd"
    assert result.output["truncated"] is True


def test_approved_subprocess_does_not_receive_secrets(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_values = {
        "OPENAI_API_KEY": "forged-openai-key",
        "TOKEN": "forged-token",
        "PASSWORD": "forged-password",
    }
    for key, value in fake_values.items():
        monkeypatch.setenv(key, value)
    (workspace / "test_environment.py").write_text(
        "import os\n"
        "import unittest\n\n"
        "class EnvironmentTest(unittest.TestCase):\n"
        "    def test_environment(self):\n"
        "        names = ('OPENAI_API_KEY', 'TOKEN', 'PASSWORD')\n"
        "        print('|'.join(os.environ.get(name, '<missing>') for name in names))\n"
        "        self.assertTrue(all(name not in os.environ for name in names))\n",
        encoding="utf-8",
    )
    executor = ToolExecutor(WorkspacePolicy(workspace, allow_commands=True))
    result = executor.execute(
        ToolCall(
            "call-env",
            "run_command",
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "unittest",
                    "-v",
                    "test_environment.py",
                ]
            },
        ),
        approved=True,
    )
    assert result.ok
    assert "<missing>|<missing>|<missing>" in result.output["stdout"]
    combined = result.output["stdout"] + result.output["stderr"]
    assert all(value not in combined for value in fake_values.values())


def test_run_command_truncates_output(workspace: Path) -> None:
    (workspace / "test_output.py").write_text(
        "import unittest\n\n"
        "class OutputTest(unittest.TestCase):\n"
        "    def test_output(self):\n"
        "        print('x' * 100)\n",
        encoding="utf-8",
    )
    executor = ToolExecutor(WorkspacePolicy(workspace, allow_commands=True), max_output_chars=20)
    result = executor.execute(
        ToolCall(
            "call-output",
            "run_command",
            {"argv": [sys.executable, "-m", "unittest", "test_output.py"]},
        ),
        approved=True,
    )
    assert result.ok
    assert result.output["truncated"] is True
    assert "输出已截断" in result.output["stdout"] + result.output["stderr"]
