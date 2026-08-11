from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pi_claw.policy import WorkspacePolicy
from pi_claw.schemas import PolicyAction, ToolCall


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall("call-1", name, arguments)


def test_policy_denies_unknown_tool(workspace: Path) -> None:
    decision = WorkspacePolicy(workspace).evaluate(call("launch_missiles"))
    assert decision.action is PolicyAction.DENY
    assert "未注册" in decision.reason


@pytest.mark.parametrize("allow_write", [False, True])
def test_write_is_always_ask(workspace: Path, allow_write: bool) -> None:
    decision = WorkspacePolicy(workspace, allow_write=allow_write).evaluate(
        call("write_or_replace", path="alpha.txt", old="alpha", new="changed")
    )
    assert decision.action is PolicyAction.ASK


@pytest.mark.parametrize("command", ["sudo", "curl", "uv", "pytest"])
def test_policy_denies_dangerous_or_unlisted_commands(workspace: Path, command: str) -> None:
    decision = WorkspacePolicy(workspace, allow_commands=True).evaluate(
        call("run_command", argv=[command, "--version"])
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
    assert decision.metadata["argv"] == argv


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
