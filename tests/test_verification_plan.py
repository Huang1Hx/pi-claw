"""宿主侧 VerificationPlan：必跑/可选、测试篡改与无关绿灯的离线验收。"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError

import pytest

from core.completion import (
    PlannedCompletionVerifier,
    ToolObservation,
    VerificationPlan,
)
from core.schemas import ToolCall, ToolResult


def observation(name, arguments, *, ok=True, output=None):
    return ToolObservation(
        ToolCall(f"{name}-call", name, arguments),
        ToolResult(f"{name}-call", name, ok, output=output),
    )


def write(path, *, ok=True):
    return observation("write_or_replace", {"path": path, "old": "a", "new": "b"}, ok=ok)


def command(argv, *, returncode=0, ok=True):
    return observation("run_command", {"argv": argv}, ok=ok, output={"returncode": returncode})


def test_plan_rejects_overlapping_or_empty_kinds() -> None:
    """必跑与可选不能重叠、不能为空，否则验收门槛可被静默降低。"""
    with pytest.raises(ValueError, match="必跑"):
        VerificationPlan(required_kinds=frozenset())
    with pytest.raises(ValueError, match="不相交"):
        VerificationPlan(required_kinds=frozenset({"test"}), optional_kinds=frozenset({"test"}))
    with pytest.raises(ValueError, match="类别"):
        VerificationPlan(required_kinds=frozenset({"unknown"}))


def test_plan_rejects_duplicate_change_mapping() -> None:
    """同一变更文件出现两次映射会让关联测试不确定，必须拒绝。"""
    with pytest.raises(ValueError, match="重复"):
        VerificationPlan(
            required_kinds=frozenset({"test"}),
            change_to_tests=(("app.py", ("test_app.py",)), ("app.py", ("test_other.py",))),
        )


def test_read_only_turn_needs_no_verification() -> None:
    check = PlannedCompletionVerifier(VerificationPlan(frozenset({"test"}))).verify(
        task="read", final_text="done", observations=[observation("read_file", {"path": "a.txt"})]
    )
    assert check.accepted and check.changed_files == ()


def test_write_to_protected_test_file_is_rejected() -> None:
    """改写受保护测试文件视为篡改，即使随后测试通过也不能算完成。"""
    verifier = PlannedCompletionVerifier(
        VerificationPlan(frozenset({"test"}), protected_patterns=("test_*.py",))
    )
    check = verifier.verify(
        task="fix",
        final_text="done",
        observations=[
            write("test_app.py"),
            command([sys.executable, "-m", "unittest", "test_app.py"]),
        ],
    )
    assert not check.accepted
    assert "受保护" in check.reason


def test_required_kind_must_pass_after_last_write() -> None:
    """只跑了可选检查（lint）不足以满足必跑（test）。"""
    verifier = PlannedCompletionVerifier(
        VerificationPlan(frozenset({"test"}), optional_kinds=frozenset({"lint"}))
    )
    only_lint = verifier.verify(
        task="fix",
        final_text="done",
        observations=[write("app.py"), command(["uv", "run", "ruff", "check", "src"])],
    )
    assert not only_lint.accepted
    assert "必跑" in only_lint.reason
    with_test = verifier.verify(
        task="fix",
        final_text="done",
        observations=[write("app.py"), command(["uv", "run", "pytest", "-q"])],
    )
    assert with_test.accepted


def test_write_after_passing_test_requires_retest() -> None:
    """测试之后又发生写入，先前绿灯不能再为完成背书。"""
    verifier = PlannedCompletionVerifier(VerificationPlan(frozenset({"test"})))
    check = verifier.verify(
        task="fix",
        final_text="done",
        observations=[
            write("app.py"),
            command(["uv", "run", "pytest", "-q"]),
            write("app.py"),
        ],
    )
    assert not check.accepted
    assert "必跑" in check.reason


def test_unrelated_green_test_is_rejected() -> None:
    """关联映射要求变更文件的测试被真实运行，跑无关测试不算数。"""
    verifier = PlannedCompletionVerifier(
        VerificationPlan(frozenset({"test"}), change_to_tests=(("app.py", ("test_app.py",)),))
    )
    unrelated = verifier.verify(
        task="fix",
        final_text="done",
        observations=[
            write("app.py"),
            command([sys.executable, "-m", "unittest", "test_other.py"]),
        ],
    )
    assert not unrelated.accepted
    assert "关联" in unrelated.reason
    related = verifier.verify(
        task="fix",
        final_text="done",
        observations=[write("app.py"), command([sys.executable, "-m", "unittest", "test_app.py"])],
    )
    assert related.accepted


def test_plan_is_immutable() -> None:
    plan = VerificationPlan(frozenset({"test"}))
    with pytest.raises(FrozenInstanceError):
        plan.required_kinds = frozenset({"lint"})
