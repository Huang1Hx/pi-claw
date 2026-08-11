from __future__ import annotations

import io

from pi_claw.approval import ApprovalChoice, TerminalApprovalBroker
from pi_claw.schemas import PolicyAction, PolicyDecision, ToolCall

DECISION = PolicyDecision(PolicyAction.ASK, "需要审批")


def write_call(path: str = "file.txt") -> ToolCall:
    return ToolCall(
        "write-1",
        "write_or_replace",
        {"path": path, "old": "a", "new": "b"},
    )


def test_approval_allow_once_does_not_persist_scope() -> None:
    broker = TerminalApprovalBroker(
        input_fn=lambda _prompt: "y",
        output=io.StringIO(),
        interactive=True,
    )
    assert broker.request(write_call(), DECISION) is ApprovalChoice.ALLOW_ONCE
    assert broker.approved_scopes == set()


def test_approval_allow_session_persists_and_skips_next_prompt() -> None:
    answers = iter(["a"])
    changes: list[set[str]] = []
    broker = TerminalApprovalBroker(
        input_fn=lambda _prompt: next(answers),
        output=io.StringIO(),
        interactive=True,
        on_state_change=changes.append,
    )
    call = write_call("target.py")

    assert broker.request(call, DECISION) is ApprovalChoice.ALLOW_SESSION
    assert broker.request(call, DECISION) is ApprovalChoice.ALLOW_SESSION
    assert broker.approved_scopes == {"write_or_replace:target.py"}
    assert changes == [{"write_or_replace:target.py"}]


def test_approval_explicit_deny() -> None:
    broker = TerminalApprovalBroker(
        input_fn=lambda _prompt: "n",
        output=io.StringIO(),
        interactive=True,
    )
    assert broker.request(write_call(), DECISION) is ApprovalChoice.DENY
    assert broker(write_call(), DECISION) is False


def test_non_tty_denies_without_reading_input() -> None:
    def must_not_prompt(_prompt: str) -> str:
        raise AssertionError("non-TTY 不得等待输入")

    broker = TerminalApprovalBroker(input_fn=must_not_prompt, interactive=False)
    assert broker.request(write_call(), DECISION) is ApprovalChoice.DENY


def test_command_scope_is_limited_to_command_prefix() -> None:
    call = ToolCall(
        "run-1",
        "run_command",
        {"argv": ["uv", "run", "pytest", "tests/test_one.py"]},
    )
    assert TerminalApprovalBroker.scope_for(call) == "run_command:uv:run:pytest"


def test_apply_patch_scope_is_limited_to_target_path() -> None:
    call = ToolCall(
        "patch-1",
        "apply_patch",
        {"path": "src/app.py", "edits": [{"old": "a", "new": "b"}]},
    )
    assert TerminalApprovalBroker.scope_for(call) == "apply_patch:src/app.py"
