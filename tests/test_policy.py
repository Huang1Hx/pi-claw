from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.schemas import PolicyAction, ToolCall
from security.policy import WorkspacePolicy


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall("call-1", name, arguments)


def test_policy_denies_unknown_tool(workspace: Path) -> None:
    decision = WorkspacePolicy(workspace).evaluate(call("launch_missiles"))
    assert decision.action is PolicyAction.DENY
    assert "未注册" in decision.reason


@pytest.mark.parametrize("allow_write", [False, True])
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_or_replace", {"path": "alpha.txt", "old": "alpha", "new": "changed"}),
        (
            "apply_patch",
            {"path": "alpha.txt", "edits": [{"old": "alpha", "new": "changed"}]},
        ),
    ],
)
def test_write_is_always_ask(
    workspace: Path,
    allow_write: bool,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    decision = WorkspacePolicy(workspace, allow_write=allow_write).evaluate(
        call(tool_name, **arguments)
    )
    assert decision.action is PolicyAction.ASK


@pytest.mark.parametrize("command", ["sudo", "curl", "pytest"])
def test_policy_denies_dangerous_or_unlisted_commands(workspace: Path, command: str) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True).evaluate(
        call("run_command", argv=[command, "--version"])
    )
    assert decision.action is PolicyAction.DENY


def test_uv_is_denied_by_minimal_profile(workspace: Path) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True).evaluate(
        call("run_command", argv=["uv", "run", "pytest", "-q"])
    )
    assert decision.action is PolicyAction.DENY


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "sync", "--locked"],
        ["uv", "sync", "--frozen"],
        ["uv", "lock", "--check"],
        ["uv", "run", "pytest", "-q", "test_sample.py"],
        ["uv", "run", "ruff", "check", "--output-format=concise", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
        ["uv", "run", "python", "-m", "unittest", "-v", "test_sample.py"],
        ["uv", "run", "python", "-m", "compileall", "-q", "."],
    ],
)
def test_coding_profile_allows_safe_uv_commands_as_ask(workspace: Path, argv: list[str]) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True, command_profile="coding").evaluate(
        call("run_command", argv=argv)
    )
    assert decision.action is PolicyAction.ASK


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "sync"],
        ["uv", "add", "requests"],
        ["uv", "run", "python", "-c", "print('unsafe')"],
        ["uv", "run", "pytest", "--collect-only"],
        ["uv", "run", "pytest", "../outside"],
        ["uv", "run", "ruff", "format", "."],
        ["uv", "run", "ruff", "check", "--fix", "."],
        ["uv", "run", "python", "-m", "compileall", "../outside"],
    ],
)
def test_coding_profile_denies_unsafe_uv_commands(workspace: Path, argv: list[str]) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True, command_profile="coding").evaluate(
        call("run_command", argv=argv)
    )
    assert decision.action is PolicyAction.DENY


@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-m", "unittest", "/tmp/test_outside.py"],
        [sys.executable, "-m", "unittest", "../test_outside.py"],
        [sys.executable, "-m", "unittest", "--locals", "test_sample.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", ".."],
        [sys.executable, "-c", "print('escape')"],
    ],
)
def test_policy_denies_unittest_path_and_option_escape(workspace: Path, argv: list[str]) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True).evaluate(
        call("run_command", argv=argv)
    )
    assert decision.action is PolicyAction.DENY


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "diff"],
        ["git", "diff", "HEAD"],
        ["git", "diff", "main..feature"],
        ["git", "diff", "--", "../outside.py"],
        ["git", "diff", "--", "/tmp/outside.py"],
        ["git", "diff", "--", ":(glob)**/*.py"],
        ["git", "diff", "--", ".env"],
    ],
)
def test_policy_denies_unsafe_git_diff(workspace: Path, argv: list[str]) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True).evaluate(
        call("run_command", argv=argv)
    )
    assert decision.action is PolicyAction.DENY


@pytest.mark.parametrize("allow_commands", [False, True])
@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-m", "unittest", "-v", "test_sample.py"],
        ["git", "status", "--short"],
        ["git", "diff", "--no-color", "--", "alpha.txt"],
    ],
)
def test_safe_commands_still_require_approval(
    workspace: Path, allow_commands: bool, argv: list[str]
) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=allow_commands).evaluate(
        call("run_command", argv=argv)
    )
    assert decision.action is PolicyAction.ASK
    expected_argv = ["python", *argv[1:]] if argv[0] == sys.executable else argv
    assert decision.metadata["argv"] == expected_argv


def test_policy_denies_command_cwd_outside_workspace(workspace: Path, tmp_path: Path) -> None:
    decision = WorkspacePolicy(workspace).evaluate(
        call("run_command", argv=["git", "status"], cwd=str(tmp_path.parent))
    )
    assert decision.action is PolicyAction.DENY


def test_policy_denies_external_absolute_executable(workspace: Path) -> None:
    decision = WorkspacePolicy(workspace).evaluate(
        call("run_command", argv=["/bin/sh", "-c", "echo unsafe"])
    )
    assert decision.action is PolicyAction.DENY
