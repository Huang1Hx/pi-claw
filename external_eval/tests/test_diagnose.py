"""坏例诊断与重试策略的离线测试（不联网、不发模型请求）。"""

from __future__ import annotations

import json
from pathlib import Path

from external_eval.diagnose import (
    BUILD_ERROR,
    EMPTY_PATCH,
    LOOP_DETECTED,
    MAX_STEPS,
    REGRESSION_ONLY,
    SOLVED,
    TARGET_TESTS_FAILED,
    classify,
    failing_tests_from_junit,
    plan_retry,
    summarize,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "swebench-verified" / "work"
    (tmp_path / "swebench-verified" / "report").mkdir(parents=True, exist_ok=True)
    return root


def _write_report(tmp_path: Path, iid: str, **over) -> None:
    data = {
        "instance_id": iid,
        "repo": "psf/requests",
        "agent": {"status": "completed", "steps": 5, "usage": {"total_tokens": 10}},
        "patch_bytes": 100,
        "evaluation": {"fail_to_pass": {"t.py::x": True}, "pass_to_pass": {"p.py::y": True}},
        "ptp_regressions": [],
        "ftp_all_passed": True,
        "relative_resolved": True,
    }
    data.update(over)
    (tmp_path / "swebench-verified" / "report" / f"{iid}.json").write_text(json.dumps(data))


def test_classify_solved_and_build_error(tmp_path):
    root = _root(tmp_path)
    _write_report(tmp_path, "a")
    assert classify({"instance_id": "a", "relative_resolved": True}, root=root)["reason"] == SOLVED
    assert (
        classify({"instance_id": "b", "error": "install failed"}, root=root)["reason"]
        == BUILD_ERROR
    )


def test_classify_max_steps_and_empty_patch(tmp_path):
    root = _root(tmp_path)
    _write_report(
        tmp_path, "a", relative_resolved=False, agent={"status": "max_steps", "steps": 30}
    )
    assert classify({"instance_id": "a"}, root=root)["reason"] == MAX_STEPS
    _write_report(tmp_path, "b", relative_resolved=False, patch_bytes=0)
    assert classify({"instance_id": "b"}, root=root)["reason"] == EMPTY_PATCH


def test_classify_loop_detected(tmp_path):
    root = _root(tmp_path)
    _write_report(
        tmp_path, "a", relative_resolved=False, agent={"status": "loop_detected", "steps": 9}
    )
    assert classify({"instance_id": "a"}, root=root)["reason"] == LOOP_DETECTED


def test_classify_target_failed_and_regression(tmp_path):
    root = _root(tmp_path)
    _write_report(
        tmp_path,
        "a",
        relative_resolved=False,
        evaluation={"fail_to_pass": {"t.py::x": False}, "pass_to_pass": {}},
        ftp_all_passed=False,
    )
    diag = classify({"instance_id": "a"}, root=root)
    assert diag["reason"] == TARGET_TESTS_FAILED
    _write_report(
        tmp_path,
        "b",
        relative_resolved=False,
        evaluation={"fail_to_pass": {"t.py::x": True}, "pass_to_pass": {}},
        ptp_regressions=["p.py::y"],
    )
    assert classify({"instance_id": "b"}, root=root)["reason"] == REGRESSION_ONLY


def test_failing_tests_from_junit_maps_and_extracts(tmp_path):
    inst = tmp_path / "i"
    repo = inst / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("")
    (inst / "junit.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x">'
        '<failure message="AssertionError: boom">trace</failure>'
        "</testcase></testsuite></testsuites>"
    )
    got = failing_tests_from_junit(inst, ["tests/test_a.py::test_x"])
    assert got == [{"test": "tests/test_a.py::test_x", "message": "AssertionError: boom"}]


def test_plan_retry_covers_each_reason():
    assert plan_retry({"reason": SOLVED}) is None
    assert plan_retry({"reason": BUILD_ERROR}) is None
    assert plan_retry({"reason": MAX_STEPS}, base_max_steps=20)["max_steps"] == 40
    hint = plan_retry(
        {"reason": TARGET_TESTS_FAILED, "failed_tests": [{"test": "t", "message": "m"}]}
    )
    assert "t" in hint["hint"]
    assert plan_retry({"reason": REGRESSION_ONLY})["hint"]
    assert plan_retry({"reason": EMPTY_PATCH})["hint"]
    assert plan_retry({"reason": LOOP_DETECTED})["hint"]


def test_summarize_collects_retryable():
    diags = [{"instance_id": "a", "reason": MAX_STEPS}, {"instance_id": "b", "reason": BUILD_ERROR}]
    summary = summarize(diags)
    assert summary["total"] == 2
    assert summary["retryable"] == ["a"]
