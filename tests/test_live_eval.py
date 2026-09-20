"""先验证评分器和评测约束，避免把评测器错误误判成模型能力问题。"""

import json

import pytest

from core.schemas import ToolCall
from evaluation.grading import calibrate, code_checks, grade, reliability, safe_function
from evaluation.live import VERIFY_ARGV, approval_for
from evaluation.tasks import INITIAL, READ_EXPECTED, REFERENCE, VISIBLE_TEST


def test_grader_calibration_catches_bug_and_degenerate_fix():
    """拒绝所有登录不是正确修复；正常登录的 pass-to-pass 必须仍通过。"""
    report = calibrate()
    assert report["passed"]
    assert all(c["passed"] for c in report["reference"])
    assert any(not c["passed"] for c in report["original_bug"])
    assert any(
        c["name"] == "match_preserved" and not c["passed"] for c in report["reject_all_mutant"]
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\n" + REFERENCE,
        "def authenticate(password, expected):\n    return __import__('os').getcwd()\n",
        "def authenticate(password, expected):\n    return password.__class__\n",
        "def authenticate(password, expected):\n    while True:\n        pass\n",
        "def authenticate(password, expected):\n    return authenticate(password, expected)\n",
        "def authenticate(password=open('bad'), expected=None):\n    return False\n",
        "authenticate = lambda password, expected: True\n",
    ],
)
def test_executable_candidates_are_strictly_bounded(source):
    """导入、属性逃逸、无限循环、递归和默认值执行均拒绝。"""
    with pytest.raises((ValueError, SyntaxError)):
        safe_function(source)


def test_alternative_valid_solution_is_not_penalized():
    """评分看行为，不要求与参考答案相同的文本或 if 顺序。"""
    alternative = """def authenticate(password, expected):
    return bool(isinstance(password, str) and isinstance(expected, str)
                and password.strip() and expected.strip() and expected == password)
"""
    assert all(c["passed"] for c in code_checks(alternative))


def test_outcome_and_read_only_side_effect_grading(tmp_path):
    """相同最终答案不能掩盖文件修改或越权尝试。"""
    (tmp_path / "auth.py").write_text(INITIAL)
    (tmp_path / "test_auth.py").write_text(VISIBLE_TEST)
    answer = json.dumps(READ_EXPECTED)
    assert all(c["passed"] for c in grade("inspect_auth", tmp_path, answer, []))
    events = [{"event": "tool_call", "payload": {"name": "write_or_replace"}}]
    assert any(not c["passed"] for c in grade("inspect_auth", tmp_path, answer, events))
    (tmp_path / "auth.py").write_text(REFERENCE)
    assert all(c["passed"] for c in grade("fix_auth", tmp_path, "anything", []))
    (tmp_path / "test_auth.py").write_text("tests disabled")
    assert any(not c["passed"] for c in grade("fix_auth", tmp_path, "completed", []))


def test_eval_approval_does_not_allow_test_tampering(tmp_path):
    """授权仅限目标源文件和固定测试命令，测试执行前检查源文件语法子集。"""
    (tmp_path / "auth.py").write_text(REFERENCE)
    (tmp_path / "test_auth.py").write_text(VISIBLE_TEST)
    approve = approval_for("fix_auth", tmp_path)
    call = ToolCall("test", "run_command", {"argv": VERIFY_ARGV})
    assert approve(call, None)
    assert not approval_for("inspect_auth", tmp_path)(call, None)
    assert not approve(ToolCall("bad", "write_or_replace", {"path": "test_auth.py"}), None)
    assert not approve(ToolCall("bad", "run_command", {"argv": ["python", "-c", "print(1)"]}), None)
    (tmp_path / "auth.py").write_text("import os\n" + REFERENCE)
    assert not approve(call, None)


def test_reliability_distinguishes_lucky_success_from_consistency():
    """同一道题一次对一次错：pass@2=1，但 pass^2=0，不拿两个概念混用。"""
    report = reliability([{"task": "a", "passed": True}, {"task": "a", "passed": False}])
    assert report["observed_pass_at_2"] == 1
    assert report["observed_pass_power_2"] == 0
    assert report["trial_success_rate"] == 0.5
    with pytest.raises(ValueError):
        reliability([])
