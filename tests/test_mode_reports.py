"""阶段二报告合并的不可变性与身份校验。"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

import evaluation.compare_modes as compare_modes
from evaluation.compare_modes import VERIFY_ARGV, build_continuation_acquisition, build_trial_report
from evaluation.mode_grading import GRADER_VERSION
from evaluation.mode_reports import (
    load_report_root,
    merge_subset,
    trial_identity,
    validate_compatible_runs,
)
from evaluation.mode_tasks import TASKS
from persistence.trace import TraceWriter


def _row(mode: str, task: str, trial: int, *, passed: bool = False) -> dict:
    """构造只含合并协议所需字段的 trial。"""
    return {
        "mode": mode,
        "task": task,
        "trial": trial,
        "passed": passed,
        "leader_status": "completed",
        "grader_version": "agent-modes-2.1",
        "workspace": f"/tmp/{mode}-{task}-{trial}/workspace",
        "duration_seconds": 1.0,
    }


def _report(rows: list[dict], *, tasks: list[str] | None = None) -> dict:
    """构造具备模型与预算边界的报告。"""
    return {
        "schema_version": 3,
        "grader_version": "agent-modes-2.1",
        "modes": ["single", "workflow", "team"],
        "tasks": tasks or ["dependency_order", "worker_assignment"],
        "trials_per_task": 3,
        "deadline_seconds": 420,
        "max_steps": 24,
        "models": {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
        },
        "premium_model": "kimi-k3",
        "trials": rows,
    }


def _successful_trial(root: Path, trial: int) -> dict:
    """写入一个可被当前评分器离线重评的真实 trial 目录。"""
    folder = root / "single" / f"inventory_reservation-{trial}"
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    task = TASKS["inventory_reservation"]
    for name, content in task.files.items():
        (workspace / name).write_text(content)
    (workspace / "solution.py").write_text(task.reference)
    (folder / "run-result.json").write_text(
        json.dumps(
            {
                "leader": {"status": "completed"},
                "subagents": [],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                    "requests": 1,
                    "requests_with_usage": 1,
                    "requests_without_usage": 0,
                    "retries": 0,
                    "unknown_participants": 0,
                },
                "duration_seconds": 1.0,
                "max_parallel_subagents": 0,
            }
        )
    )
    trace = TraceWriter(folder / "trace-leader.jsonl")
    trace.record("run_started", {"task": task.prompt})
    trace.record(
        "tool_result",
        {
            "call_id": "verify",
            "tool_name": "run_command",
            "ok": True,
            "output": {"argv": VERIFY_ARGV, "returncode": 0},
            "error": None,
            "metadata": {},
        },
    )
    trace.record("run_completed", {"final_text": "done"})
    return build_trial_report(
        folder,
        {
            "mode": "single",
            "task": "inventory_reservation",
            "trial": trial,
            "duration_seconds": 1.0,
        },
    )


def test_load_report_root_returns_exact_report_file_hash(tmp_path):
    """来源指纹必须来自原始 report.json 字节，不能对解析后对象重新序列化。"""
    root = tmp_path / "run"
    root.mkdir()
    raw = b'{"trials": [], "custom_spacing": true}\n'
    (root / "report.json").write_bytes(raw)

    report, path, digest = load_report_root(root)

    assert report == {"trials": [], "custom_spacing": True}
    assert path == (root / "report.json").resolve()
    assert digest == hashlib.sha256(raw).hexdigest()


def test_merge_subset_replaces_exact_identity_and_regrades_every_retained_trial():
    """子集补跑只能替换同身份项，其余项必须按当前评分器重新评分。"""
    source_rows = [
        _row("single", "dependency_order", 1),
        _row("workflow", "dependency_order", 1),
        _row("team", "worker_assignment", 1),
    ]
    replacement_row = {
        **source_rows[1],
        "passed": True,
        "grader_version": GRADER_VERSION,
        "workspace": "/tmp/replacement/workspace",
    }
    regraded = []

    def regrade(row: dict) -> dict:
        regraded.append(trial_identity(row))
        return {**row, "grader_version": GRADER_VERSION, "regraded": True}

    rows, metadata = merge_subset(
        _report(source_rows),
        _report([replacement_row], tasks=["dependency_order"]),
        regrade=regrade,
    )

    assert [trial_identity(row) for row in rows] == [
        ("single", "dependency_order", 1),
        ("workflow", "dependency_order", 1),
        ("team", "worker_assignment", 1),
    ]
    assert rows[1] == replacement_row
    assert regraded == [
        ("single", "dependency_order", 1),
        ("team", "worker_assignment", 1),
    ]
    assert metadata == {
        "replacement_trials": 1,
        "retained_trials_regraded": 2,
        "replaced_identities": [{"mode": "workflow", "task": "dependency_order", "trial": 1}],
    }


@pytest.mark.parametrize(
    ("source_rows", "replacement_rows", "message"),
    [
        (
            [_row("single", "dependency_order", 1)],
            [
                _row("single", "dependency_order", 1),
                _row("single", "dependency_order", 1),
            ],
            "重复",
        ),
        (
            [_row("single", "dependency_order", 1)],
            [_row("team", "dependency_order", 1)],
            "来源之外",
        ),
    ],
)
def test_merge_subset_rejects_duplicate_or_unknown_replacement_identity(
    source_rows, replacement_rows, message
):
    """重复身份与来源外身份都会破坏一对一血缘，必须立即失败。"""
    with pytest.raises(ValueError, match=message):
        merge_subset(
            _report(source_rows),
            _report(replacement_rows, tasks=["dependency_order"]),
            regrade=lambda row: row,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("models", {"coder": "different"}),
        ("premium_model", "different-premium"),
        ("trials_per_task", 2),
        ("deadline_seconds", 300),
        ("max_steps", 12),
    ],
)
def test_validate_compatible_runs_rejects_different_model_or_budget(field, value):
    """模型或 trial 预算变化会混入第二个实验变量，不能直接合并。"""
    source = _report([_row("single", "dependency_order", 1)])
    replacement = _report([_row("single", "dependency_order", 1)], tasks=["dependency_order"])
    replacement[field] = value

    with pytest.raises(ValueError, match=field):
        validate_compatible_runs(source, replacement)


def test_load_report_root_rejects_missing_or_non_object_report(tmp_path):
    """路径错误和 JSON 顶层类型错误必须在创建输出目录前暴露。"""
    with pytest.raises(ValueError, match="report.json"):
        load_report_root(tmp_path / "missing")
    root = tmp_path / "list-report"
    root.mkdir()
    (root / "report.json").write_text(json.dumps([]))
    with pytest.raises(ValueError, match="对象"):
        load_report_root(root)


def test_offline_merge_cli_regrades_retained_trials_without_loading_model_config(
    tmp_path, monkeypatch
):
    """离线合并不应读取 API 配置，且必须重新生成完整聚合报告与来源指纹。"""
    source_root = tmp_path / "source"
    replacement_root = tmp_path / "replacement"
    source_rows = [_successful_trial(source_root, 1), _successful_trial(source_root, 2)]
    replacement_row = _successful_trial(replacement_root, 2)
    common = {
        "schema_version": 3,
        "grader_version": GRADER_VERSION,
        "modes": ["single"],
        "tasks": ["inventory_reservation"],
        "trials_per_task": 2,
        "deadline_seconds": 420,
        "max_steps": 24,
        "models": {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
        },
        "premium_model": "kimi-k3",
    }
    source_root.mkdir(exist_ok=True)
    replacement_root.mkdir(exist_ok=True)
    (source_root / "report.json").write_text(json.dumps({**common, "trials": source_rows}))
    (replacement_root / "report.json").write_text(
        json.dumps({**common, "trials": [replacement_row]})
    )
    output = tmp_path / "merged"

    def forbidden_config_load(*_args, **_kwargs):
        raise AssertionError("离线合并不应加载模型配置")

    monkeypatch.setattr(compare_modes, "load_settings", forbidden_config_load)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--output",
            str(output),
            "--merge-from",
            str(source_root),
            "--replace-from",
            str(replacement_root),
        ],
    )

    assert compare_modes.main() == 0
    report = json.loads((output / "report.json").read_text())
    assert len(report["trials"]) == 2
    assert report["merge"]["replacement_trials"] == 1
    assert report["merge"]["retained_trials_regraded"] == 1
    assert report["merge"]["source_report_sha256"]
    assert report["merge"]["replacement_report_sha256"]
    assert report["by_mode"]["single"]["strict_successes"] == 2
    assert report["grader_version"] == GRADER_VERSION


def test_offline_merge_rejects_replacement_not_scored_by_current_grader(
    tmp_path, monkeypatch, capsys
):
    """替换项不会再次执行模型，因此其报告和逐项评分器版本都必须是当前口径。"""
    source_root = tmp_path / "source"
    replacement_root = tmp_path / "replacement"
    source_root.mkdir()
    replacement_root.mkdir()
    source_row = _row("single", "dependency_order", 1)
    replacement_row = {**source_row, "passed": True}
    (source_root / "report.json").write_text(json.dumps(_report([source_row])))
    (replacement_root / "report.json").write_text(
        json.dumps(_report([replacement_row], tasks=["dependency_order"]))
    )
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--output",
            str(output),
            "--merge-from",
            str(source_root),
            "--replace-from",
            str(replacement_root),
        ],
    )

    with pytest.raises(SystemExit):
        compare_modes.main()

    assert not output.exists()
    assert "替换报告评分器" in capsys.readouterr().err


def test_continuation_acquisition_counts_original_failures_and_only_new_retry_cost():
    """恢复总成本应包含原失败请求与新补跑，不能重复计算被保留的成功 trial。"""
    retained = {
        **_row("single", "dependency_order", 1, passed=True),
        "usage": {"total_tokens": 10},
        "usage_may_be_incomplete": False,
        "duration_seconds": 1.0,
    }
    failed = {
        **_row("workflow", "dependency_order", 1),
        "leader_status": "provider_error",
        "usage": {"total_tokens": 5},
        "usage_may_be_incomplete": True,
        "duration_seconds": 2.0,
    }
    retry = {
        **failed,
        "leader_status": "completed",
        "passed": True,
        "usage": {"total_tokens": 20},
        "usage_may_be_incomplete": False,
        "duration_seconds": 3.0,
    }

    acquisition = build_continuation_acquisition(
        {"trials": [retained, failed]},
        {"trials": [retained, retry]},
    )

    assert acquisition["source"]["total_known_tokens"] == 15
    assert acquisition["retry"]["total_known_tokens"] == 20
    assert acquisition["total_known_tokens"] == 35
    assert acquisition["total_duration_seconds"] == 6.0
    assert acquisition["unknown_usage_trials"] == 1


def test_continuation_acquisition_rejects_missing_or_extra_retry_identity():
    """完成报告必须覆盖原报告全部身份，且只把原 provider error 当作新采集。"""
    failed = {
        **_row("single", "dependency_order", 1),
        "leader_status": "provider_error",
        "usage": None,
        "usage_may_be_incomplete": True,
    }
    with pytest.raises(ValueError, match="身份集合"):
        build_continuation_acquisition(
            {"trials": [failed]},
            {"trials": []},
        )
