"""阶段 3 新任务 holdout：未参与调试的新任务必须同协议可校准、可评分、可独立运行。"""

from __future__ import annotations

import json
import sys

import pytest

from evaluation import compare_modes
from evaluation.mode_cases import HIDDEN_CASES
from evaluation.mode_grading import calibrate, grade
from evaluation.mode_holdout_tasks import ALL_TASKS, HOLDOUT_TASKS
from evaluation.mode_tasks import TASKS


def _seed(tmp_path, task, source):
    workspace = tmp_path / task.task_id
    workspace.mkdir(parents=True)
    for name, content in task.files.items():
        (workspace / name).write_text(content)
    (workspace / "solution.py").write_text(source)
    return workspace


def test_holdout_set_is_disjoint_from_debugged_core_tasks():
    """holdout 至少 3 项，且不得复用本阶段调试过的核心任务。"""
    assert len(HOLDOUT_TASKS) >= 3
    assert set(HOLDOUT_TASKS).isdisjoint(TASKS)
    assert set(ALL_TASKS) == set(TASKS) | set(HOLDOUT_TASKS)
    assert all(1 <= len(task.parameters) <= 4 for task in HOLDOUT_TASKS.values())


def test_holdout_hidden_cases_exist_and_are_well_formed():
    """每项 holdout 都要有隐藏用例，且用例为名称/类别/参数/期望四元组。"""
    for task_id in HOLDOUT_TASKS:
        cases = HIDDEN_CASES[task_id]
        assert cases
        for name, category, arguments, _expected in cases:
            assert name and category in {"pass_to_pass", "fail_to_pass"}
            assert isinstance(arguments, tuple)


def test_holdout_calibration_passes_reference_and_rejects_initial():
    """参考实现必须全过隐藏用例，初始缺陷必须至少被一个用例检出。"""
    report = calibrate(task_ids=list(HOLDOUT_TASKS))
    assert report["passed"] is True
    assert set(report["tasks"]) == set(HOLDOUT_TASKS)
    for row in report["tasks"].values():
        assert all(check["passed"] is True for check in row["reference"])
        assert any(check["passed"] is False for check in row["initial"])


def test_holdout_core_calibration_stays_at_ten_tasks():
    """默认校准仍只覆盖核心 10 项，holdout 不污染默认回归口径。"""
    report = calibrate()
    assert set(report["tasks"]) == set(TASKS)
    assert len(report["tasks"]) == 10


def test_holdout_reference_is_graded_passed_and_initial_is_not(tmp_path):
    """同评分器下参考实现通过全部检查，初始缺陷不能伪装通过。"""
    for task in HOLDOUT_TASKS.values():
        reference = grade(task.task_id, _seed(tmp_path / "ref", task, task.reference), "completed")
        assert all(check["passed"] is True for check in reference), task.task_id
        initial = grade(task.task_id, _seed(tmp_path / "init", task, task.initial), "completed")
        assert not all(check["passed"] is True for check in initial), task.task_id


def test_cli_holdout_runs_only_new_task_set_offline(tmp_path, monkeypatch):
    """`--holdout` 离线校准只覆盖新任务集，不触碰核心 10 项。"""
    output = tmp_path / "holdout"
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare_modes", "--holdout", "--output", str(output)],
    )
    assert compare_modes.main() == 0
    calibration = json.loads((output / "grader-calibration.json").read_text())
    assert set(calibration["tasks"]) == set(HOLDOUT_TASKS)
    assert calibration["passed"] is True


def test_cli_rejects_holdout_combined_with_task_selection(tmp_path, monkeypatch):
    """显式任务与 holdout 任务集互斥，避免误把 holdout 混进核心批次。"""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--holdout",
            "--task",
            "merge_sorted_streams",
            "--output",
            str(tmp_path / "conflict"),
        ],
    )
    with pytest.raises(SystemExit):
        compare_modes.main()
