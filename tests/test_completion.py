from __future__ import annotations

import sys

import pytest

from pi_claw.completion import ToolEvidenceCompletionVerifier, ToolObservation
from pi_claw.schemas import ToolCall, ToolResult


def observation(
    name: str,
    arguments: dict[str, object],
    *,
    ok: bool = True,
    output: object = None,
) -> ToolObservation:
    return ToolObservation(
        ToolCall(f"{name}-call", name, arguments),
        ToolResult(f"{name}-call", name, ok, output=output),
    )


def test_completion_verifier_accepts_read_only_turn() -> None:
    check = ToolEvidenceCompletionVerifier().verify(
        task="read",
        final_text="done",
        observations=[observation("read_file", {"path": "alpha.txt"})],
    )

    assert check.accepted
    assert check.changed_files == ()
    assert check.checks == ()


def test_completion_verifier_requires_check_after_last_successful_write() -> None:
    verifier = ToolEvidenceCompletionVerifier()
    before = observation(
        "run_command",
        {"argv": [sys.executable, "-m", "unittest", "test_sample.py"]},
        output={"returncode": 0},
    )
    write = observation(
        "write_or_replace",
        {"path": "alpha.txt", "old": "alpha", "new": "changed"},
    )

    check = verifier.verify(task="change", final_text="done", observations=[before, write])

    assert not check.accepted
    assert check.changed_files == ("alpha.txt",)
    assert "没有运行" in check.reason


def test_completion_verifier_accepts_successful_uv_test_after_write() -> None:
    write = observation(
        "apply_patch",
        {"path": "app.py", "edits": [{"old": "a", "new": "b"}]},
    )
    test = observation(
        "run_command",
        {"argv": ["uv", "run", "pytest", "-q"]},
        output={"returncode": 0},
    )

    check = ToolEvidenceCompletionVerifier().verify(
        task="fix", final_text="done", observations=[write, test]
    )

    assert check.accepted
    assert check.checks[0]["kind"] == "test"
    assert check.checks[0]["ok"] is True


def test_completion_verifier_rejects_failed_check() -> None:
    write = observation(
        "write_or_replace",
        {"path": "app.py", "old": "a", "new": "b"},
    )
    failed = observation(
        "run_command",
        {"argv": ["uv", "run", "ruff", "check", "src"]},
        ok=False,
        output={"returncode": 1},
    )

    check = ToolEvidenceCompletionVerifier().verify(
        task="fix", final_text="done", observations=[write, failed]
    )

    assert not check.accepted
    assert "均未通过" in check.reason


def test_completion_verifier_requires_at_least_one_accepted_kind() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ToolEvidenceCompletionVerifier(accepted_kinds=frozenset())
