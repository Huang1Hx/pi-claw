from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluation.runner import EvalRunner, load_eval_suite, write_eval_report


def test_default_offline_suite_runs_and_passes(tmp_path: Path) -> None:
    suite = load_eval_suite()
    report = EvalRunner(suite, traces_dir=tmp_path / "traces").run()

    assert report.passed
    assert report.provider == "scripted"
    assert report.network_used is False
    assert report.metrics["total_cases"] == 8
    assert report.metrics["passed_cases"] == 8
    assert report.metrics["pass_rate"] == 1.0
    assert report.metrics["security_pass_rate"] == 1.0
    assert report.metrics["security_violations"] == 0
    assert report.metrics["assertion_failures"] == 0
    assert {case.case_id for case in report.cases} == {
        "fix_and_verify",
        "recover_after_test_failure",
        "deny_unapproved_write",
        "deny_path_escape",
        "safe_max_steps",
        "isolation_worker_a",
        "isolation_worker_b",
        "reject_unverified_completion",
    }
    assert all(Path(case.trace_path).is_file() for case in report.cases)
    assert len({case.isolation_id for case in report.cases}) == 8
    assert report.metrics["task_isolation_violations"] == 0
    assert report.metrics["completion_rejections"] == 1
    assert report.metrics["unverified_runs"] == 1
    assert report.metrics["verified_completions"] == 6


def test_eval_runs_cases_concurrently_with_isolated_workspaces(tmp_path: Path) -> None:
    report = EvalRunner(load_eval_suite(), traces_dir=tmp_path / "traces").run(workers=3)

    assert report.passed
    assert report.metrics["execution_backend"] == "thread"
    assert report.metrics["workers_requested"] == 3
    assert report.metrics["workers_used"] == 3
    assert report.metrics["max_parallel_cases"] >= 2
    assert report.metrics["task_isolation_violations"] == 0
    assert report.metrics["wall_duration_ms"] > 0
    assert report.metrics["serial_work_ms"] > 0
    assert report.metrics["observed_speedup"] > 0
    assert len({case.isolation_id for case in report.cases}) == len(report.cases)


@pytest.mark.parametrize("workers", [0, -1, True])
def test_eval_rejects_invalid_worker_count(tmp_path: Path, workers: Any) -> None:
    runner = EvalRunner(load_eval_suite(), traces_dir=tmp_path / "traces")

    with pytest.raises(ValueError, match="workers 必须是正整数"):
        runner.run(workers=workers)


def test_eval_reports_expected_failure_recovery_and_denials(tmp_path: Path) -> None:
    report = EvalRunner(load_eval_suite(), traces_dir=tmp_path / "traces").run()
    cases = {case.case_id: case for case in report.cases}

    recovery = cases["recover_after_test_failure"]
    assert recovery.passed
    assert recovery.non_ok_tool_results == 1
    assert recovery.successful_tool_calls == 3
    assert cases["deny_unapproved_write"].approval_denials == 1
    assert cases["deny_path_escape"].policy_denials == 1
    assert cases["safe_max_steps"].run_status == "max_steps"
    unverified = cases["reject_unverified_completion"]
    assert unverified.passed
    assert unverified.run_status == "unverified"
    assert unverified.completion_rejections == 1
    assert unverified.completion_verified is False


def test_eval_case_filter_and_unknown_case(tmp_path: Path) -> None:
    runner = EvalRunner(load_eval_suite(), traces_dir=tmp_path / "traces")

    report = runner.run(case_ids={"deny_path_escape"})
    assert [case.case_id for case in report.cases] == ["deny_path_escape"]

    with pytest.raises(ValueError, match="未知评测 case"):
        runner.run(case_ids={"missing"})


def test_eval_report_marks_changed_expectation_as_failure(tmp_path: Path) -> None:
    suite = copy.deepcopy(load_eval_suite())
    suite["cases"] = [suite["cases"][0]]
    suite["cases"][0]["expect"]["file_contains"] = {"calculator.py": "return definitely_wrong"}

    report = EvalRunner(suite, traces_dir=tmp_path / "traces").run()

    assert not report.passed
    assert report.metrics["pass_rate"] == 0.0
    assert report.metrics["assertion_failures"] == 1
    assert any(
        assertion.name == "file_contains:calculator.py" and not assertion.passed
        for assertion in report.cases[0].assertions
    )


def test_eval_condition_proves_tool_result_is_in_next_model_context(tmp_path: Path) -> None:
    suite = copy.deepcopy(load_eval_suite())
    recovery = next(case for case in suite["cases"] if case["id"] == "recover_after_test_failure")
    recovery["replies"][1]["require_last_tool_result"]["ok"] = True
    suite["cases"] = [recovery]

    report = EvalRunner(suite, traces_dir=tmp_path / "traces").run()

    assert not report.passed
    assert report.cases[0].run_status == "provider_error"
    assert any(
        assertion.name == "run_status" and not assertion.passed
        for assertion in report.cases[0].assertions
    )


def test_isolation_case_proves_own_file_content_reaches_model_context(tmp_path: Path) -> None:
    suite = copy.deepcopy(load_eval_suite())
    isolation = next(case for case in suite["cases"] if case["id"] == "isolation_worker_a")
    isolation["replies"][1]["require_last_tool_result"]["output_contains"] = "worker-b-only"
    suite["cases"] = [isolation]

    report = EvalRunner(suite, traces_dir=tmp_path / "traces").run()

    assert not report.passed
    assert report.cases[0].run_status == "provider_error"


def test_eval_suite_rejects_fixture_path_traversal(tmp_path: Path) -> None:
    suite = copy.deepcopy(load_eval_suite())
    suite["cases"][0]["files"] = {"../escape.py": "unsafe"}
    suite_path = tmp_path / "unsafe-suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")

    with pytest.raises(ValueError, match="安全相对路径"):
        load_eval_suite(suite_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda case: case.update(max_steps=0), "max_steps"),
        (lambda case: case.update(policy={"auto_approve": "yes"}), "auto_approve"),
        (lambda case: case.update(replies=[{"tool_calls": [{"name": "read_file"}]}]), "id"),
        (lambda case: case["expect"].update(tool_results=[{"tool": "read_file"}]), "ok"),
    ],
)
def test_eval_suite_rejects_malformed_case_schema(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    suite = copy.deepcopy(load_eval_suite())
    mutation(suite["cases"][0])
    suite_path = tmp_path / "malformed-suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_eval_suite(suite_path)


def test_write_eval_report_is_valid_json_and_leaves_no_temp_file(tmp_path: Path) -> None:
    report = EvalRunner(load_eval_suite(), traces_dir=tmp_path / "traces").run(
        case_ids={"safe_max_steps"}
    )
    target = write_eval_report(report, tmp_path / "reports" / "eval.json")

    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["suite_name"] == "pi-claw-offline-baseline"
    assert value["metrics"]["passed_cases"] == 1
    assert list(target.parent.glob("*.tmp")) == []
