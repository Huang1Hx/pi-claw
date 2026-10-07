"""Stage 3 冻结 12 项诊断复测的身份、提示与聚合契约。"""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluation.compare_modes as compare_modes
from evaluation.mode_diagnostic import (
    FROZEN_STAGE3_FAILURES,
    build_diagnostic_prompt,
    load_diagnostic_grade_subset,
    merge_diagnostic_rows,
    merge_post_repair_diagnostic_rows,
    select_diagnostic_jobs,
    select_failed_diagnostic_jobs,
    summarize_diagnostic_acquisition,
    summarize_diagnostic_results,
)
from evaluation.mode_repair import build_repair_evidence
from evaluation.mode_tasks import TASKS


def _source_and_ledger(tmp_path: Path) -> tuple[dict, dict]:
    unrecovered = []
    entries = []
    for mode, task, trial, status in FROZEN_STAGE3_FAILURES:
        workspace = tmp_path / mode / f"{task}-{trial}"
        workspace.mkdir(parents=True)
        (workspace / "solution.py").write_text("def candidate():\n    return None\n")
        trace = workspace.parent / f"{task}-{trial}.jsonl"
        trace.write_text("")
        row = {
            "mode": mode,
            "task": task,
            "trial": trial,
            "passed": False,
            "leader_status": status,
            "workspace": str(workspace),
            "traces": [str(trace)],
            "failure_checks": [{"name": "strict", "layer": "L2_semantics"}],
        }
        unrecovered.append({"mode": mode, "task": task, "trial": trial, "status": status})
        entries.append(
            {
                "identity": {"mode": mode, "task": task, "trial": trial},
                "rounds": [row],
                "final_passed": False,
                "stop_reason": "round_limit",
            }
        )
    source = {
        "grader_version": "agent-modes-2.2",
        "deadline_seconds": 420,
        "max_steps": 24,
        "workers": 1,
        "repair_source": {"max_rounds": 2},
        "repair": {
            "post_repair": {"strict_successes": 78, "strict_failures": 12},
            "unrecovered": unrecovered,
        },
    }
    return source, {"entries": entries}


def _diagnostic_source_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    """创建带完整血缘、模型与传输设置的冻结修复来源。"""
    source, ledger = _source_and_ledger(tmp_path / "candidates")
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline_path = baseline_root / "report.json"
    baseline_path.write_text(json.dumps({"grader_version": "agent-modes-2.2"}))
    baseline_digest = sha256(baseline_path.read_bytes()).hexdigest()
    source.update(
        {
            "schema_version": 4,
            "models": {
                "planner": "deepseek-flash",
                "coder": "deepseek-flash",
                "reviewer": "deepseek-flash",
            },
            "premium_model": "kimi-k3",
            "transport": {
                "max_attempts_per_request": 3,
                "retry_base_delay_seconds": 2.0,
                "max_retry_delay_seconds": 10.0,
            },
            "repair_source": {
                "report": str(baseline_path),
                "report_sha256": baseline_digest,
                "max_rounds": 2,
            },
        }
    )
    ledger.update(
        {
            "schema_version": 1,
            "source_report": str(baseline_path),
            "source_report_sha256": baseline_digest,
        }
    )
    source_root = tmp_path / "repair-source"
    source_root.mkdir()
    source_path = source_root / "report.json"
    ledger_path = source_root / "repair-ledger.json"
    source_path.write_text(json.dumps(source))
    ledger_path.write_text(json.dumps(ledger))
    return source_root, source_path, ledger_path


def test_frozen_stage3_identity_and_status_distribution() -> None:
    assert len(FROZEN_STAGE3_FAILURES) == 12
    assert len({item[:3] for item in FROZEN_STAGE3_FAILURES}) == 12
    statuses = [item[3] for item in FROZEN_STAGE3_FAILURES]
    assert statuses.count("deadline") == 8
    assert statuses.count("loop_detected") == 2
    assert statuses.count("provider_error") == 1
    assert statuses.count("unverified") == 1


def test_select_diagnostic_jobs_requires_exact_frozen_final_candidates(tmp_path: Path) -> None:
    source, ledger = _source_and_ledger(tmp_path)

    jobs = select_diagnostic_jobs(source, ledger)

    assert [(*job.identity, job.source_row["leader_status"]) for job in jobs] == list(
        FROZEN_STAGE3_FAILURES
    )
    assert all(job.source_solution.is_file() for job in jobs)


def test_select_diagnostic_jobs_rejects_identity_or_status_drift(tmp_path: Path) -> None:
    source, ledger = _source_and_ledger(tmp_path)
    source["repair"]["unrecovered"][0]["trial"] = 99
    with pytest.raises(ValueError, match="冻结身份"):
        select_diagnostic_jobs(source, ledger)

    source, ledger = _source_and_ledger(tmp_path / "status")
    ledger["entries"][0]["rounds"][-1]["leader_status"] = "completed"
    with pytest.raises(ValueError, match="终态"):
        select_diagnostic_jobs(source, ledger)


def test_diagnostic_prompt_is_explicitly_not_a_third_baseline_repair(tmp_path: Path) -> None:
    source, ledger = _source_and_ledger(tmp_path)
    job = select_diagnostic_jobs(source, ledger)[0]
    evidence = build_repair_evidence(job.source_row)

    prompt = build_diagnostic_prompt(job, evidence)

    assert "独立诊断复测" in prompt
    assert "不合并" in prompt and "78/90" in prompt
    assert "第 3" not in prompt and "第三轮" not in prompt
    assert "只能修改 solution.py" in prompt
    assert "uv run python -m unittest -q test_target.py" in prompt
    assert str(job.source_workspace) not in prompt


def test_diagnostic_trial_uses_diagnostic_metadata_not_repair_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    """独立复测的目录元数据和提示文件不能伪装成第三轮 repair。"""
    source, ledger = _source_and_ledger(tmp_path)
    job = select_diagnostic_jobs(source, ledger)[0]
    captured = {}

    def fake_run_trial(mode, task, trial, args, **kwargs):
        captured.update({"mode": mode, "task": task, "trial": trial, "args": args, **kwargs})
        return {"passed": False}

    monkeypatch.setattr(compare_modes, "run_trial", fake_run_trial)
    args = SimpleNamespace(output=tmp_path / "diagnostic")

    assert compare_modes.run_diagnostic_trial(job, args) == {"passed": False}
    assert captured["trial_metadata_label"] == "diagnostic"
    assert captured["prompt_filename"] == "diagnostic-prompt.txt"
    assert captured["trial_metadata"]["merge_into_baseline"] is False
    assert "独立诊断复测" in captured["prompt"]


def _diagnostic_rows(tmp_path: Path) -> list[dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, (mode, task, trial, _status) in enumerate(FROZEN_STAGE3_FAILURES):
        trace = tmp_path / f"trace-{index}.jsonl"
        trace.write_text("")
        rows.append(
            {
                "mode": mode,
                "task": task,
                "trial": trial,
                "passed": index < 2,
                "leader_status": "completed" if index < 2 else "deadline",
                "passed_checks": 15 if index < 2 else 3,
                "total_checks": 15,
                "duration_seconds": 10.0,
                "usage": {"total_tokens": 100},
                "usage_may_be_incomplete": False,
                "failure_checks": [] if index < 2 else [{"layer": "L0_execution"}],
                "traces": [str(trace)],
                "workspace": str(tmp_path / f"workspace-{index}"),
            }
        )
    return rows


def test_diagnostic_summary_is_quantitative_and_drops_private_paths(tmp_path: Path) -> None:
    rows = _diagnostic_rows(tmp_path)

    summary = summarize_diagnostic_results(rows)

    assert summary["trial_count"] == 12
    assert summary["strict_successes"] == 2
    assert summary["strict_success_rate"] == 0.166667
    assert summary["deadline_or_loop_before"] == 10
    assert summary["deadline_or_loop_after"] == 10
    assert summary["deadline_or_loop_target_met"] is False
    assert summary["request_budget_provider_misclassifications"] == 0
    assert summary["known_total_tokens"] == 1200
    assert summary["total_duration_seconds"] == 120.0
    serialized = json.dumps(summary)
    assert str(tmp_path) not in serialized
    assert "workspace" not in serialized and "traces" not in serialized


def test_diagnostic_summary_detects_request_budget_provider_misclassification(
    tmp_path: Path,
) -> None:
    rows = _diagnostic_rows(tmp_path)
    target = rows[0]
    target["passed"] = False
    target["leader_status"] = "provider_error"
    trace = Path(target["traces"][0])
    trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {"error": "已达到本回合模型请求上限"},
            }
        )
        + "\n"
    )

    summary = summarize_diagnostic_results(rows)

    assert summary["request_budget_provider_misclassifications"] == 1
    assert summary["provider_classification_target_met"] is False


def test_partial_diagnostic_grades_replace_exact_identities_and_have_digest(tmp_path):
    """部分重跑只覆盖同身份行，目录指纹固定实际 grade.json 字节。"""
    source = _diagnostic_rows(tmp_path / "source")
    replacement_root = tmp_path / "retry"
    replacements = []
    for index in (1, 4, 7):
        row = {
            **source[index],
            "passed": True,
            "leader_status": "completed",
            "usage": {"total_tokens": 250},
            "failure_checks": [],
        }
        grade = replacement_root / row["mode"] / f"{row['task']}-{row['trial']}" / "grade.json"
        grade.parent.mkdir(parents=True)
        grade.write_text(json.dumps(row))
        replacements.append(row)

    loaded, digest = load_diagnostic_grade_subset(replacement_root)
    merged = merge_diagnostic_rows(source, loaded)

    assert len(digest) == 64
    assert [compare_modes.trial_identity(row) for row in loaded] == [
        compare_modes.trial_identity(row) for row in replacements
    ]
    assert len(merged) == 12
    assert sum(row["passed"] is True for row in merged) == 4
    assert source[4]["passed"] is False  # 原列表不被函数原地改写。
    assert merged[4]["usage"]["total_tokens"] == 250


def test_partial_diagnostic_grades_reject_duplicate_or_unknown_identity(tmp_path):
    source = _diagnostic_rows(tmp_path / "source")
    duplicate = [source[0], dict(source[0])]
    with pytest.raises(ValueError, match="重复"):
        merge_diagnostic_rows(source, duplicate)
    unknown = {**source[0], "task": "dependency_order"}
    with pytest.raises(ValueError, match="不在来源"):
        merge_diagnostic_rows(source, [unknown])


def test_select_failed_diagnostic_jobs_includes_provider_and_deadline(tmp_path):
    """诊断续跑允许瞬时 Provider 错误和 deadline，但只选当前仍失败的五项。"""
    rows = _diagnostic_rows(tmp_path / "rows")
    for index, row in enumerate(rows):
        workspace = Path(row["workspace"])
        workspace.mkdir(parents=True)
        (workspace / "solution.py").write_text("def candidate():\n    return None\n")
        row["passed"] = index < 7
        row["leader_status"] = (
            "completed" if index < 7 else ("provider_error" if index < 10 else "deadline")
        )

    jobs = select_failed_diagnostic_jobs(rows, expected_count=5)

    assert [job.identity for job in jobs] == [compare_modes.trial_identity(row) for row in rows[7:]]
    assert {job.source_row["leader_status"] for job in jobs} == {
        "provider_error",
        "deadline",
    }


def test_diagnostic_acquisition_counts_every_attempt_without_double_counting(tmp_path):
    source = _diagnostic_rows(tmp_path / "source")
    prior = [{**row, "usage": {"total_tokens": 200}} for row in source[:6]]
    retry = [{**row, "usage": {"total_tokens": 300}} for row in source[7:]]

    acquisition = summarize_diagnostic_acquisition(
        [("source", source), ("prior_retry", prior), ("retry", retry)]
    )

    assert acquisition["total_trial_runs"] == 23
    assert acquisition["total_known_tokens"] == 3900
    assert acquisition["total_duration_seconds"] == 230.0
    assert [stage["trial_runs"] for stage in acquisition["stages"]] == [12, 6, 5]


def test_merge_post_repair_diagnostic_rows_builds_separate_90_trial_result(tmp_path):
    """诊断只允许替换修复后仍失败的冻结 12 项，并保留完整 90 行。"""
    frozen = {item[:3] for item in FROZEN_STAGE3_FAILURES}
    post_rows = []
    for task in TASKS:
        for trial in range(1, 4):
            for mode in ("single", "workflow", "team"):
                identity = (mode, task, trial)
                post_rows.append(
                    {
                        "mode": mode,
                        "task": task,
                        "trial": trial,
                        "passed": identity not in frozen,
                        "leader_status": "completed" if identity not in frozen else "deadline",
                    }
                )
    diagnostics = [
        {
            "mode": mode,
            "task": task,
            "trial": trial,
            "passed": True,
            "leader_status": "completed",
        }
        for mode, task, trial, _status in FROZEN_STAGE3_FAILURES
    ]

    merged = merge_post_repair_diagnostic_rows(post_rows, diagnostics)

    assert len(merged) == 90
    assert sum(row["passed"] is True for row in merged) == 90
    assert sum(row["passed"] is True for row in post_rows) == 78


def test_diagnostic_cli_requires_live_and_rejects_competing_modes(tmp_path, monkeypatch):
    """独立诊断必须显式联网，且不能与续跑、修复或身份筛选混用。"""
    source_root, _source_path, _ledger_path = _diagnostic_source_root(tmp_path)
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--output",
            str(output),
            "--diagnostic-from",
            str(source_root),
        ],
    )
    with pytest.raises(SystemExit):
        compare_modes.main()
    assert not output.exists()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--live",
            "--output",
            str(output),
            "--diagnostic-from",
            str(source_root),
            "--mode",
            "single",
        ],
    )
    with pytest.raises(SystemExit):
        compare_modes.main()
    assert not output.exists()


def test_diagnostic_report_is_separate_exact_and_source_immutable(tmp_path, monkeypatch):
    """12 项各运行一次；来源字节不变，公开摘要不能泄露路径或 Trace。"""
    source_root, source_path, ledger_path = _diagnostic_source_root(tmp_path)
    source_digest = sha256(source_path.read_bytes()).hexdigest()
    ledger_digest = sha256(ledger_path.read_bytes()).hexdigest()
    output = tmp_path / "diagnostic"
    profiles = {
        name: SimpleNamespace(model=model)
        for name, model in {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
            "premium": "kimi-k3",
        }.items()
    }
    settings = SimpleNamespace(
        workflow=SimpleNamespace(
            enabled=True,
            planner="planner",
            coder="coder",
            reviewer="reviewer",
            premium_reviewer="premium",
        ),
        profiles=profiles,
    )
    captured = {}

    def execute(jobs, args):
        captured["identities"] = [job.identity for job in jobs]
        captured["protocol"] = (
            args.workers,
            args.deadline,
            args.max_steps,
            args.max_attempts,
            args.retry_base_delay,
            args.max_retry_delay,
        )
        return _diagnostic_rows(tmp_path)

    monkeypatch.setattr(compare_modes, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(compare_modes, "calibrate", lambda: {"passed": True})
    monkeypatch.setattr(compare_modes, "execute_diagnostic_jobs", execute)
    args = SimpleNamespace(
        config=tmp_path / "pi-claw.toml",
        output=output,
        workers=3,
        deadline=30,
        max_steps=4,
        max_attempts=1,
        retry_base_delay=0.0,
        max_retry_delay=0.0,
    )

    report = compare_modes.run_diagnostic_report(source_root, args)

    assert captured["identities"] == [item[:3] for item in FROZEN_STAGE3_FAILURES]
    assert captured["protocol"] == (1, 420, 24, 3, 2.0, 10.0)
    assert report["diagnostic"]["trial_count"] == 12
    assert report["diagnostic"]["strict_successes"] == 2
    assert report["diagnostic_lineage"]["merge_into_baseline"] is False
    assert report["diagnostic_lineage"]["source_report_sha256"] == source_digest
    assert report["diagnostic_lineage"]["source_ledger_sha256"] == ledger_digest
    assert sha256(source_path.read_bytes()).hexdigest() == source_digest
    assert sha256(ledger_path.read_bytes()).hexdigest() == ledger_digest
    public = json.loads((output / "public-summary.json").read_text())
    serialized = json.dumps(public)
    assert public["diagnostic"]["trial_count"] == 12
    assert str(tmp_path) not in serialized
    assert "traces" not in serialized and "workspace" not in serialized


def test_diagnostic_validation_fails_before_creating_output(tmp_path, monkeypatch):
    """模型或来源协议漂移时 fail closed，不能留下半成品目录。"""
    source_root, source_path, _ledger_path = _diagnostic_source_root(tmp_path)
    source = json.loads(source_path.read_text())
    source["workers"] = 2
    source_path.write_text(json.dumps(source))
    monkeypatch.setattr(
        compare_modes,
        "load_settings",
        lambda **_kwargs: pytest.fail("来源协议错误时不应加载模型配置"),
    )
    output = tmp_path / "diagnostic"

    with pytest.raises(ValueError, match="workers=1"):
        compare_modes.run_diagnostic_report(
            source_root,
            SimpleNamespace(config=tmp_path / "pi-claw.toml", output=output),
        )

    assert not output.exists()


def _continuation_tree(tmp_path: Path) -> tuple[Path, Path, Path, list[dict]]:
    """构造 78/90 修复来源、4/12 诊断和 3/6 成功的部分重跑。"""
    frozen = {item[:3] for item in FROZEN_STAGE3_FAILURES}
    diagnostic_rows = _diagnostic_rows(tmp_path / "diagnostic-rows")
    for index, row in enumerate(diagnostic_rows):
        workspace = Path(row["workspace"])
        workspace.mkdir(parents=True)
        (workspace / "solution.py").write_text("def candidate():\n    return None\n")
        row["passed"] = index in {0, 9, 10, 11}
        row["leader_status"] = (
            "completed"
            if row["passed"]
            else ("provider_error" if index in {1, 2, 4, 5, 6, 7} else "deadline")
        )
        row["failure_checks"] = [] if row["passed"] else [{"layer": "L0_execution"}]

    baseline_rows = []
    diagnostic_map = {compare_modes.trial_identity(row): row for row in diagnostic_rows}
    for task in TASKS:
        for trial in range(1, 4):
            for mode in ("single", "workflow", "team"):
                identity = (mode, task, trial)
                source = diagnostic_map.get(identity)
                source_row = dict(source) if source is not None else None
                if source_row is not None:
                    source_row["passed"] = False
                    source_row["leader_status"] = "deadline"
                baseline_rows.append(
                    source_row
                    if source_row is not None
                    else {
                        "mode": mode,
                        "task": task,
                        "trial": trial,
                        "passed": True,
                        "leader_status": "completed",
                    }
                )
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline_path = baseline_root / "report.json"
    baseline = {
        "grader_version": "agent-modes-2.2",
        "modes": ["single", "workflow", "team"],
        "tasks": list(TASKS),
        "trials_per_task": 3,
        "trials": baseline_rows,
    }
    baseline_path.write_text(json.dumps(baseline))
    baseline_digest = sha256(baseline_path.read_bytes()).hexdigest()

    repair_root = tmp_path / "repair"
    repair_root.mkdir()
    repair_report_path = repair_root / "report.json"
    repair_report = {
        "grader_version": "agent-modes-2.2",
        "models": {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
        },
        "premium_model": "kimi-k3",
        "repair_source": {
            "report": str(baseline_path),
            "report_sha256": baseline_digest,
            "max_rounds": 2,
        },
        "repair": {
            "post_repair": {
                "strict_successes": 78,
                "strict_failures": 12,
                "strict_success_rate": 0.866667,
            },
            "total_acquisition_cost": {
                "known_tokens": 1000,
                "duration_seconds": 200.0,
                "unknown_usage_trials": 1,
            },
            "unrecovered": [],
        },
    }
    repair_report_path.write_text(json.dumps(repair_report))
    repair_report_digest = sha256(repair_report_path.read_bytes()).hexdigest()
    entries = []
    for mode, task, trial, _status in FROZEN_STAGE3_FAILURES:
        row = dict(diagnostic_map[(mode, task, trial)])
        row["passed"] = False
        entries.append(
            {
                "identity": {"mode": mode, "task": task, "trial": trial},
                "rounds": [row],
                "final_passed": False,
            }
        )
    ledger_path = repair_root / "repair-ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "source_report": str(baseline_path),
                "source_report_sha256": baseline_digest,
                "entries": entries,
            }
        )
    )
    ledger_digest = sha256(ledger_path.read_bytes()).hexdigest()

    diagnostic_root = tmp_path / "diagnostic"
    diagnostic_root.mkdir()
    diagnostic_report = {
        "schema_version": 5,
        "grader_version": "agent-modes-2.2",
        "workers": 1,
        "deadline_seconds": 1200,
        "max_steps": 24,
        "models": repair_report["models"],
        "premium_model": "kimi-k3",
        "transport": {
            "max_attempts_per_request": 3,
            "retry_base_delay_seconds": 2.0,
            "max_retry_delay_seconds": 10.0,
        },
        "diagnostic_lineage": {
            "source_report": str(repair_report_path),
            "source_report_sha256": repair_report_digest,
            "source_ledger": str(ledger_path),
            "source_ledger_sha256": ledger_digest,
            "merge_into_baseline": False,
        },
        "trials": diagnostic_rows,
    }
    diagnostic_path = diagnostic_root / "report.json"
    diagnostic_path.write_text(json.dumps(diagnostic_report))

    retry_root = tmp_path / "retry6"
    for index in (1, 2, 4, 5, 6, 7):
        row = dict(diagnostic_rows[index])
        row["passed"] = index in {2, 5, 7}
        row["leader_status"] = "completed" if row["passed"] else "provider_error"
        row["failure_checks"] = [] if row["passed"] else [{"layer": "L0_execution"}]
        row["usage"] = {"total_tokens": 200}
        grade = retry_root / row["mode"] / f"{row['task']}-{row['trial']}" / "grade.json"
        grade.parent.mkdir(parents=True)
        grade.write_text(json.dumps(row))
    assert {identity for identity in diagnostic_map if identity in frozen} == frozen
    return diagnostic_root, retry_root, diagnostic_path, diagnostic_rows


def test_diagnostic_continuation_reruns_exact_five_and_builds_derived_90(tmp_path, monkeypatch):
    """4/12＋3/6 后只跑剩余五项，并把最新 12 项覆盖到独立的 90 行报告。"""
    source_root, retry_root, source_path, diagnostic_rows = _continuation_tree(tmp_path)
    source_digest = sha256(source_path.read_bytes()).hexdigest()
    retry_rows, retry_digest = load_diagnostic_grade_subset(retry_root)
    output = tmp_path / "retry5"
    settings = SimpleNamespace(
        workflow=SimpleNamespace(
            enabled=True,
            planner="planner",
            coder="coder",
            reviewer="reviewer",
            premium_reviewer="premium",
        ),
        profiles={
            "planner": SimpleNamespace(model="deepseek-flash"),
            "coder": SimpleNamespace(model="deepseek-flash"),
            "reviewer": SimpleNamespace(model="deepseek-flash"),
            "premium": SimpleNamespace(model="kimi-k3"),
        },
    )
    captured = {}

    def execute(jobs, args, *, expected_count=12):
        captured["identities"] = [job.identity for job in jobs]
        captured["expected_count"] = expected_count
        captured["protocol"] = (args.workers, args.deadline, args.max_steps)
        rows = []
        for job in jobs:
            source = dict(job.source_row)
            source.update(
                {
                    "passed": True,
                    "leader_status": "completed",
                    "failure_checks": [],
                    "usage": {"total_tokens": 300},
                    "usage_may_be_incomplete": False,
                    "duration_seconds": 30.0,
                }
            )
            rows.append(source)
        return rows

    monkeypatch.setattr(compare_modes, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(compare_modes, "calibrate", lambda: {"passed": True})
    monkeypatch.setattr(compare_modes, "execute_diagnostic_jobs", execute)
    monkeypatch.setattr(
        compare_modes,
        "summarize",
        lambda rows, **_kwargs: {
            "strict_successes": sum(row.get("passed") is True for row in rows),
            "strict_failures": sum(row.get("passed") is not True for row in rows),
        },
    )
    args = SimpleNamespace(
        config=tmp_path / "pi-claw.toml",
        output=output,
        expected_diagnostic_failures=5,
        expected_diagnostic_replacements=6,
    )

    report = compare_modes.run_diagnostic_continuation(source_root, retry_root, args)

    assert captured["identities"] == [
        compare_modes.trial_identity(diagnostic_rows[index]) for index in (1, 3, 4, 6, 8)
    ]
    assert captured["expected_count"] == 5
    assert captured["protocol"] == (1, 1200, 24)
    assert len(report["trials"]) == 90
    assert sum(row["passed"] is True for row in report["trials"]) == 90
    assert report["diagnostic"]["strict_successes"] == 12
    assert report["continuation"]["source_post_repair_successes"] == 78
    assert report["continuation"]["derived_successes"] == 90
    assert report["acquisition"]["diagnostic"]["total_trial_runs"] == 23
    assert report["diagnostic_lineage"]["prior_replacements_sha256"] == retry_digest
    assert sha256(source_path.read_bytes()).hexdigest() == source_digest
    assert load_diagnostic_grade_subset(retry_root)[1] == retry_digest
    public = json.loads((output / "public-summary.json").read_text())
    serialized = json.dumps(public)
    assert public["continuation"]["derived_successes"] == 90
    assert str(tmp_path) not in serialized
    assert "traces" not in serialized and "workspace" not in serialized


def test_diagnostic_continuation_cli_requires_live(tmp_path, monkeypatch):
    source = tmp_path / "source"
    retry = tmp_path / "retry"
    source.mkdir()
    retry.mkdir()
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--output",
            str(output),
            "--continue-diagnostic-from",
            str(source),
            "--diagnostic-replacements-from",
            str(retry),
            "--expected-diagnostic-failures",
            "5",
            "--expected-diagnostic-replacements",
            "6",
        ],
    )

    with pytest.raises(SystemExit):
        compare_modes.main()

    assert not output.exists()


def test_diagnostic_continuation_validates_historical_cost_before_model_calls(
    tmp_path, monkeypatch
):
    """历史成本缺失时必须在校准和模型调用前失败，不能留下半成品目录。"""
    source_root, retry_root, source_path, _rows = _continuation_tree(tmp_path)
    source = json.loads(source_path.read_text())
    repair_path = Path(source["diagnostic_lineage"]["source_report"])
    repair = json.loads(repair_path.read_text())
    repair["repair"].pop("total_acquisition_cost")
    repair_path.write_text(json.dumps(repair))
    source["diagnostic_lineage"]["source_report_sha256"] = sha256(
        repair_path.read_bytes()
    ).hexdigest()
    source_path.write_text(json.dumps(source))
    monkeypatch.setattr(
        compare_modes,
        "_settings_models",
        lambda _args: (
            {
                "planner": "deepseek-flash",
                "coder": "deepseek-flash",
                "reviewer": "deepseek-flash",
            },
            "kimi-k3",
        ),
    )
    monkeypatch.setattr(
        compare_modes,
        "calibrate",
        lambda: pytest.fail("历史血缘无效时不应校准或调用模型"),
    )
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="历史总成本"):
        compare_modes.run_diagnostic_continuation(
            source_root,
            retry_root,
            SimpleNamespace(
                config=tmp_path / "pi-claw.toml",
                output=output,
                expected_diagnostic_failures=5,
                expected_diagnostic_replacements=6,
            ),
        )

    assert not output.exists()
