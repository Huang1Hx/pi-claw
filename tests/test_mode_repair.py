"""Trace 驱动候选修复的选择、脱敏与干净工作区测试。"""

import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluation.compare_modes as compare_modes
from evaluation.compare_modes import (
    execute_repair_rounds,
    resume_repair_transport,
    run_repair_round,
)
from evaluation.mode_repair import (
    build_repair_evidence,
    build_repair_prompt,
    is_transient_provider_error,
    prepare_repair_workspace,
    select_repair_jobs,
    summarize_repairs,
)
from evaluation.mode_tasks import TASKS


def _candidate(tmp_path: Path, *, task: str = "dependency_order") -> Path:
    """创建含候选代码的来源工作区。"""
    workspace = tmp_path / "source" / "workspace"
    workspace.mkdir(parents=True)
    for name, content in TASKS[task].files.items():
        (workspace / name).write_text(content)
    (workspace / "solution.py").write_text(
        "def dependency_order(graph):\n    return list(graph)\n"
        if task == "dependency_order"
        else TASKS[task].initial
    )
    return workspace


def _row(workspace: Path, *, status: str = "completed", passed: bool = False) -> dict:
    """构造一个带稳定身份与评分失败的来源行。"""
    return {
        "mode": "workflow",
        "task": "dependency_order",
        "trial": 2,
        "passed": passed,
        "leader_status": status,
        "workspace": str(workspace),
        "traces": [],
        "failure_checks": [
            {
                "name": "dependencies_must_be_list",
                "layer": "L2_semantics",
                "passed": False,
                "detail": "expected=['secret']; actual=('secret',)",
            }
        ],
    }


def test_select_repair_jobs_keeps_only_failed_candidates_and_preserves_identity(tmp_path):
    """成功与传输/框架错误不能进入语义修复，其他有候选文件的失败可修复。"""
    workspace = _candidate(tmp_path)
    eligible = _row(workspace)
    passed = {**eligible, "mode": "single", "trial": 1, "passed": True}
    provider = {
        **eligible,
        "mode": "team",
        "trial": 1,
        "leader_status": "provider_error",
    }
    harness = {
        **eligible,
        "mode": "single",
        "trial": 3,
        "leader_status": "harness_error",
    }

    jobs = select_repair_jobs({"trials": [passed, provider, harness, eligible]})

    assert len(jobs) == 1
    assert jobs[0].identity == ("workflow", "dependency_order", 2)
    assert jobs[0].source_workspace == workspace.resolve()
    assert jobs[0].source_solution.read_text().endswith("return list(graph)\n")


def test_select_repair_jobs_rejects_duplicate_identity_or_missing_solution(tmp_path):
    """重复血缘和没有候选源码的失败都不能进入自动修复。"""
    workspace = _candidate(tmp_path)
    row = _row(workspace)
    with pytest.raises(ValueError, match="重复"):
        select_repair_jobs({"trials": [row, dict(row)]})

    (workspace / "solution.py").unlink()
    with pytest.raises(ValueError, match="solution.py"):
        select_repair_jobs({"trials": [row]})


def test_repair_evidence_keeps_actionable_errors_but_removes_hidden_values_and_secrets(
    tmp_path,
):
    """反馈只保留公开错误与失败类别，不泄漏隐藏答案、连接信息和本机路径。"""
    workspace = _candidate(tmp_path)
    trace = tmp_path / "trace-leader.jsonl"
    events = [
        {
            "event": "tool_result",
            "step": 3,
            "payload": {
                "tool_name": "run_command",
                "ok": False,
                "output": {
                    "argv": ["uv", "run", "python", "-m", "unittest"],
                    "stdout": (
                        "public test failed at /Users/huanghx/private/test_target.py "
                        "and /opt/private/hidden.py"
                    ),
                    "stderr": "AssertionError: topological order invalid",
                },
                "error": "未获批准：不允许的语法节点：While",
                "api_key": "sk-super-secret-token",
                "base_url": "https://llm.ustc.edu.cn/v1",
            },
        },
        {
            "event": "completion_rejected",
            "step": 4,
            "payload": {
                "reason": "expected=['hidden-node']; actual=['wrong-node']",
            },
        },
        {
            "event": "model_reply",
            "step": 5,
            "payload": {"text": "这段完整模型回复不得进入修复证据"},
        },
    ]
    trace.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
    row = {**_row(workspace), "traces": [str(trace)]}

    evidence = build_repair_evidence(row, max_chars=8000)
    text = json.dumps(evidence, ensure_ascii=False)

    assert "dependencies_must_be_list" in text
    assert "L2_semantics" in text
    assert "topological order invalid" in text
    assert "不允许的语法节点：While" in text
    assert "完整模型回复" not in text
    assert "hidden-node" not in text
    assert "wrong-node" not in text
    assert "expected" not in text.lower()
    assert "actual" not in text.lower()
    assert "sk-super-secret-token" not in text
    assert "llm.ustc.edu.cn" not in text
    assert "/Users/" not in text
    assert "/opt" not in text
    assert len(text) <= 8000


def test_repair_evidence_obeys_small_total_budget(tmp_path):
    """单条超长公开输出也不能突破整个反馈对象的字符预算。"""
    workspace = _candidate(tmp_path)
    trace = tmp_path / "trace-leader.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "tool_result",
                "step": 1,
                "payload": {
                    "tool_name": "run_command",
                    "ok": False,
                    "output": {"stderr": "x" * 20_000},
                    "error": None,
                },
            }
        )
        + "\n"
    )

    evidence = build_repair_evidence({**_row(workspace), "traces": [str(trace)]}, max_chars=600)

    assert len(json.dumps(evidence, ensure_ascii=False)) <= 600
    assert evidence["truncated"] is True


def test_prepare_repair_workspace_restores_catalog_fixtures_and_only_copies_solution(
    tmp_path,
):
    """来源 fixture 即使被污染，新一轮也只能继承候选 solution.py。"""
    workspace = _candidate(tmp_path)
    candidate = (workspace / "solution.py").read_text()
    (workspace / "SPEC.md").write_text("污染后的规格")
    (workspace / "test_target.py").write_text("raise SystemExit('polluted')")
    (workspace / "extra-secret.txt").write_text("do not copy")
    job = select_repair_jobs({"trials": [_row(workspace)]})[0]
    destination = tmp_path / "repair" / "workspace"

    prepared = prepare_repair_workspace(job, destination)

    assert prepared == destination.resolve()
    assert (prepared / "SPEC.md").read_text() == TASKS["dependency_order"].spec
    assert (prepared / "test_target.py").read_text() == TASKS["dependency_order"].public_test
    assert (prepared / "solution.py").read_text() == candidate
    assert not (prepared / "extra-secret.txt").exists()


def test_repair_prompt_limits_scope_and_identifies_round(tmp_path):
    """修复提示必须明确只改候选文件，并标识当前轮次与脱敏证据。"""
    job = select_repair_jobs({"trials": [_row(_candidate(tmp_path))]})[0]
    evidence = {
        "status": "completed",
        "failure_checks": [{"name": "dependencies_must_be_list", "layer": "L2_semantics"}],
        "events": [],
        "truncated": False,
    }

    prompt = build_repair_prompt(job, evidence, round_number=2)

    assert "第 2/2 轮" in prompt
    assert "solution.py" in prompt
    assert "只能修改" in prompt
    assert "dependencies_must_be_list" in prompt
    assert "隐藏" in prompt
    assert "uv run python -m unittest -q test_target.py" in prompt


def test_run_repair_round_preserves_identity_mode_seed_and_parent_hash(tmp_path, monkeypatch):
    """一轮修复必须沿用原架构与身份，并把父候选和血缘元数据交给 trial runner。"""
    job = select_repair_jobs({"trials": [_row(_candidate(tmp_path))]})[0]
    captured = {}
    expected = {**job.source_row, "passed": True}

    def fake_run_trial(mode, task, trial, args, **kwargs):
        captured.update({"mode": mode, "task": task, "trial": trial, "args": args, **kwargs})
        return expected

    monkeypatch.setattr(compare_modes, "run_trial", fake_run_trial)
    args = SimpleNamespace(output=tmp_path / "repairs")

    assert run_repair_round(job, 1, args) == expected
    assert (captured["mode"], captured["task"], captured["trial"]) == job.identity
    assert captured["folder"] == (
        tmp_path / "repairs" / "round-1" / "workflow" / "dependency_order-2"
    )
    assert captured["seed_solution"] == job.source_solution.read_text()
    assert "第 1/2 轮" in captured["prompt"]
    assert captured["trial_metadata"]["parent_solution_sha256"]
    assert captured["trial_metadata"]["round"] == 1


def test_execute_repair_rounds_stops_after_success_and_never_exceeds_limit(tmp_path):
    """每个身份成功即停；继续失败的身份最多执行用户配置的两轮。"""
    workspace = _candidate(tmp_path)
    first = _row(workspace)
    second = {**first, "mode": "team", "trial": 1}
    calls = []

    def fake_runner(job, round_number, args):
        calls.append((*job.identity, round_number))
        result_workspace = (
            tmp_path / "results" / f"{job.mode}-{job.task}-{job.trial}-{round_number}" / "workspace"
        )
        result_workspace.mkdir(parents=True)
        (result_workspace / "solution.py").write_text(job.source_solution.read_text())
        passed = job.mode == "workflow" or round_number == 2
        return {
            **job.source_row,
            "workspace": str(result_workspace),
            "traces": [],
            "passed": passed,
            "leader_status": "completed",
            "usage": {"total_tokens": 10},
            "usage_may_be_incomplete": False,
            "duration_seconds": 1.0,
            "failure_checks": [] if passed else job.source_row["failure_checks"],
        }

    ledger = execute_repair_rounds(
        {"trials": [first, second]},
        SimpleNamespace(output=tmp_path / "repairs", workers=2, repair_rounds=2),
        runner=fake_runner,
    )

    by_mode = {entry["identity"]["mode"]: entry for entry in ledger}
    assert len(by_mode["workflow"]["rounds"]) == 1
    assert len(by_mode["team"]["rounds"]) == 2
    assert all(entry["final_passed"] is True for entry in ledger)
    assert calls.count(("workflow", "dependency_order", 2, 1)) == 1
    assert ("workflow", "dependency_order", 2, 2) not in calls
    assert max(call[-1] for call in calls) == 2

    with pytest.raises(ValueError, match="1–2"):
        execute_repair_rounds(
            {"trials": [first]},
            SimpleNamespace(output=tmp_path / "invalid", workers=1, repair_rounds=3),
            runner=fake_runner,
        )


def test_execute_repair_rounds_does_not_treat_provider_error_as_semantic_feedback(
    tmp_path,
):
    """某轮重试后仍是传输失败时必须停止该身份，不能用错误内容驱动下一轮。"""
    row = _row(_candidate(tmp_path))
    calls = []

    def provider_failure(job, round_number, args):
        calls.append(round_number)
        return {
            **job.source_row,
            "leader_status": "provider_error",
            "passed": False,
            "usage": None,
            "usage_may_be_incomplete": True,
            "duration_seconds": 1.0,
        }

    ledger = execute_repair_rounds(
        {"trials": [row]},
        SimpleNamespace(output=tmp_path / "repairs", workers=1, repair_rounds=2),
        runner=provider_failure,
    )

    assert calls == [1]
    assert ledger[0]["stop_reason"] == "provider_error"
    assert ledger[0]["final_passed"] is False


def _metric_row(
    mode: str,
    trial: int,
    *,
    passed: bool,
    tokens: int | None,
    status: str = "completed",
    layer: str = "L2_semantics",
    unknown: bool = False,
) -> dict:
    """构造修复汇总所需的独立手工样本。"""
    return {
        "mode": mode,
        "task": "dependency_order",
        "trial": trial,
        "passed": passed,
        "leader_status": status,
        "usage": None if tokens is None else {"total_tokens": tokens},
        "usage_may_be_incomplete": unknown,
        "duration_seconds": 2.0,
        "failure_checks": []
        if passed
        else [{"name": "invalid_order", "layer": layer, "passed": False}],
        "traces": [f"/trace/{mode}-{trial}.jsonl"],
        "unauthorized_attempts": 1 if mode == "team" else 0,
        "write_conflicts": 0,
        "rework_events": 1 if mode == "workflow" else 0,
        "model_requests": {"kimi-k3": 1} if mode == "team" else {},
    }


def test_summarize_repairs_separates_first_pass_post_repair_and_all_incremental_costs():
    """恢复率不能洗掉首次失败，分母还必须包含失败轮次和未知 Token 缺口。"""
    baseline_rows = [
        _metric_row("single", 1, passed=True, tokens=100),
        _metric_row("workflow", 1, passed=False, tokens=200),
        _metric_row("team", 1, passed=False, tokens=300),
        _metric_row(
            "single",
            2,
            passed=False,
            tokens=None,
            status="provider_error",
            layer="L0_execution",
            unknown=True,
        ),
    ]
    workflow_round_1 = _metric_row("workflow", 1, passed=False, tokens=50)
    workflow_round_2 = _metric_row("workflow", 1, passed=True, tokens=70)
    team_round_1 = _metric_row("team", 1, passed=True, tokens=80, unknown=True)
    ledger = [
        {
            "identity": {"mode": "workflow", "task": "dependency_order", "trial": 1},
            "baseline": baseline_rows[1],
            "rounds": [workflow_round_1, workflow_round_2],
            "final_passed": True,
            "stop_reason": "passed",
        },
        {
            "identity": {"mode": "team", "task": "dependency_order", "trial": 1},
            "baseline": baseline_rows[2],
            "rounds": [team_round_1],
            "final_passed": True,
            "stop_reason": "passed",
        },
    ]
    baseline = {
        "modes": ["single", "workflow", "team"],
        "trials": baseline_rows,
        "acquisition": {
            "total_known_tokens": 1000,
            "total_duration_seconds": 20.0,
            "unknown_usage_trials": 1,
        },
    }

    summary = summarize_repairs(baseline, ledger, premium_model="kimi-k3")

    assert summary["first_pass"] == {
        "strict_successes": 1,
        "strict_failures": 3,
        "strict_success_rate": 0.25,
    }
    assert summary["post_repair"]["strict_successes"] == 3
    assert summary["post_repair"]["strict_success_rate"] == 0.75
    assert summary["eligible_failures"] == 2
    assert summary["recovered_trials"] == 2
    assert summary["recovery_rate"] == 1.0
    assert summary["recovered_by_round"] == {"1": 1, "2": 1}
    assert summary["repair_cost"]["known_tokens"] == 200
    assert summary["repair_cost"]["unknown_usage_trials"] == 1
    assert summary["repair_cost"]["known_tokens_per_recovered_trial"] == 100
    assert summary["total_acquisition_cost"]["known_tokens"] == 1200
    assert summary["premium_model_requests"] == 1
    assert summary["unrecovered"][0]["status"] == "provider_error"
    assert summary["unrecovered_failure_layers"] == {"L0_execution": 1}


def test_repair_metrics_reject_ledger_that_omits_an_eligible_failure():
    """修复账本不能漏掉可修复身份，否则恢复率分母会被静默缩小。"""
    failed = _metric_row("workflow", 1, passed=False, tokens=10)

    with pytest.raises(ValueError, match="未完整覆盖"):
        summarize_repairs(
            {"modes": ["workflow"], "trials": [failed]},
            [],
        )


def test_transient_provider_error_uses_trace_and_rejects_request_budget_exhaustion(tmp_path):
    """连接重置可做传输续跑；内部请求预算耗尽不是瞬时 API 故障。"""
    transient_trace = tmp_path / "transient.jsonl"
    transient_trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {
                    "error": "Provider 错误：模型请求失败：[Errno 54] Connection reset by peer"
                },
            }
        )
        + "\n"
    )
    budget_trace = tmp_path / "budget.jsonl"
    budget_trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {"error": "Provider 错误：已达到本回合模型请求上限"},
            }
        )
        + "\n"
    )

    assert is_transient_provider_error(
        {"leader_status": "provider_error", "traces": [str(transient_trace)]}
    )
    assert not is_transient_provider_error(
        {"leader_status": "provider_error", "traces": [str(budget_trace)]}
    )


def test_resume_repair_transport_retries_same_round_and_preserves_failed_cost(
    tmp_path,
):
    """传输续跑替换同一语义轮次，但旧网络失败仍计入 superseded 成本。"""
    workspace = _candidate(tmp_path)
    baseline_row = {
        **_row(workspace),
        "usage": {"total_tokens": 10},
        "usage_may_be_incomplete": False,
        "duration_seconds": 1.0,
    }
    trace = tmp_path / "provider.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {"error": "Connection reset by peer"},
            }
        )
        + "\n"
    )
    provider_failure = {
        **baseline_row,
        "leader_status": "provider_error",
        "traces": [str(trace)],
        "usage": {"total_tokens": 20},
        "usage_may_be_incomplete": True,
        "duration_seconds": 2.0,
    }
    ledger = [
        {
            "identity": {"mode": "workflow", "task": "dependency_order", "trial": 2},
            "baseline": baseline_row,
            "rounds": [provider_failure],
            "final_passed": False,
            "stop_reason": "provider_error",
        }
    ]
    calls = []

    def successful_retry(job, round_number, args):
        calls.append((job.identity, round_number))
        return {
            **job.source_row,
            "passed": True,
            "leader_status": "completed",
            "usage": {"total_tokens": 30},
            "usage_may_be_incomplete": False,
            "duration_seconds": 3.0,
            "failure_checks": [],
            "traces": [],
        }

    resumed = resume_repair_transport(
        {"trials": [baseline_row]},
        ledger,
        SimpleNamespace(output=tmp_path / "continued", workers=1, repair_rounds=2),
        runner=successful_retry,
    )

    assert calls == [(("workflow", "dependency_order", 2), 1)]
    assert resumed[0]["rounds"][0]["passed"] is True
    assert resumed[0]["superseded_transport_attempts"] == [provider_failure]
    assert resumed[0]["stop_reason"] == "passed"
    summary = summarize_repairs({"modes": ["workflow"], "trials": [baseline_row]}, resumed)
    assert summary["repair_attempts"] == 2
    assert summary["repair_cost"]["known_tokens"] == 50


def test_resume_repair_transport_does_not_retry_non_transient_provider_error(tmp_path):
    """请求预算耗尽保留原结论，不能再次产生高成本模型调用。"""
    workspace = _candidate(tmp_path)
    baseline_row = _row(workspace)
    trace = tmp_path / "budget.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {"error": "已达到本回合模型请求上限"},
            }
        )
        + "\n"
    )
    failed = {
        **baseline_row,
        "leader_status": "provider_error",
        "traces": [str(trace)],
    }
    ledger = [
        {
            "identity": {"mode": "workflow", "task": "dependency_order", "trial": 2},
            "baseline": baseline_row,
            "rounds": [failed],
            "final_passed": False,
            "stop_reason": "provider_error",
        }
    ]

    resumed = resume_repair_transport(
        {"trials": [baseline_row]},
        ledger,
        SimpleNamespace(output=tmp_path / "continued", workers=1, repair_rounds=2),
        runner=lambda *_args: pytest.fail("非瞬时错误不应重跑"),
    )

    assert resumed == ledger


def test_repair_cli_requires_explicit_live_flag(tmp_path, monkeypatch):
    """只给来源路径不能触发模型请求，自动修复必须再次显式选择 live。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text(json.dumps({"trials": []}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--output",
            str(tmp_path / "output"),
            "--repair-from",
            str(source),
        ],
    )

    with pytest.raises(SystemExit):
        compare_modes.main()
    assert not (tmp_path / "output").exists()


def test_repair_cli_writes_immutable_ledger_and_separate_metrics(tmp_path, monkeypatch):
    """修复批次要固定来源哈希，并分别输出首次和修复后指标。"""
    workspace = _candidate(tmp_path)
    baseline_row = {
        **_row(workspace),
        "usage": {"total_tokens": 20},
        "usage_may_be_incomplete": False,
        "duration_seconds": 2.0,
        "unauthorized_attempts": 0,
        "write_conflicts": 0,
        "rework_events": 0,
        "model_requests": {},
    }
    baseline = {
        "schema_version": 4,
        "grader_version": "agent-modes-2.2",
        "modes": ["workflow"],
        "tasks": ["dependency_order"],
        "trials_per_task": 1,
        "workers": 1,
        "deadline_seconds": 420,
        "max_steps": 24,
        "models": {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
        },
        "premium_model": "kimi-k3",
        "trials": [baseline_row],
    }
    source = tmp_path / "baseline"
    source.mkdir()
    report_path = source / "report.json"
    report_path.write_text(json.dumps(baseline))
    original_digest = sha256(report_path.read_bytes()).hexdigest()
    output = tmp_path / "repairs"

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
    repaired = {
        **baseline_row,
        "passed": True,
        "failure_checks": [],
        "usage": {"total_tokens": 30},
        "duration_seconds": 3.0,
    }

    monkeypatch.setattr(compare_modes, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        compare_modes,
        "execute_repair_rounds",
        lambda _baseline, _args: [
            {
                "identity": {
                    "mode": "workflow",
                    "task": "dependency_order",
                    "trial": 2,
                },
                "baseline": baseline_row,
                "rounds": [repaired],
                "final_passed": True,
                "stop_reason": "passed",
            }
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--live",
            "--output",
            str(output),
            "--repair-from",
            str(source),
            "--workers",
            "1",
        ],
    )

    assert compare_modes.main() == 0
    report = json.loads((output / "report.json").read_text())
    ledger = json.loads((output / "repair-ledger.json").read_text())
    assert report["repair"]["first_pass"]["strict_successes"] == 0
    assert report["repair"]["post_repair"]["strict_successes"] == 1
    assert report["repair"]["repair_cost"]["known_tokens"] == 30
    assert ledger["source_report_sha256"] == original_digest
    assert ledger["entries"][0]["stop_reason"] == "passed"
    assert sha256(report_path.read_bytes()).hexdigest() == original_digest


def test_repair_resume_cli_writes_new_lineage_without_overwriting_source(tmp_path, monkeypatch):
    """修复传输续跑必须写新目录，并固定 baseline、旧报告和旧账本三重指纹。"""
    workspace = _candidate(tmp_path)
    baseline_row = {
        **_row(workspace),
        "usage": {"total_tokens": 10},
        "usage_may_be_incomplete": False,
        "duration_seconds": 1.0,
    }
    baseline = {
        "schema_version": 4,
        "grader_version": "agent-modes-2.2",
        "modes": ["workflow"],
        "tasks": ["dependency_order"],
        "trials_per_task": 1,
        "deadline_seconds": 420,
        "max_steps": 24,
        "models": {
            "planner": "deepseek-flash",
            "coder": "deepseek-flash",
            "reviewer": "deepseek-flash",
        },
        "premium_model": "kimi-k3",
        "trials": [baseline_row],
    }
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline_path = baseline_root / "report.json"
    baseline_path.write_text(json.dumps(baseline))
    baseline_digest = sha256(baseline_path.read_bytes()).hexdigest()

    trace = tmp_path / "provider.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "provider_error",
                "payload": {"error": "Connection reset by peer"},
            }
        )
        + "\n"
    )
    failed = {
        **baseline_row,
        "leader_status": "provider_error",
        "traces": [str(trace)],
        "usage": {"total_tokens": 5},
        "usage_may_be_incomplete": True,
    }
    entries = [
        {
            "identity": {"mode": "workflow", "task": "dependency_order", "trial": 2},
            "baseline": baseline_row,
            "rounds": [failed],
            "final_passed": False,
            "stop_reason": "provider_error",
        }
    ]
    source_root = tmp_path / "repair-source"
    source_root.mkdir()
    source_report = {
        **{
            key: baseline[key]
            for key in (
                "schema_version",
                "grader_version",
                "modes",
                "tasks",
                "trials_per_task",
                "deadline_seconds",
                "max_steps",
                "models",
                "premium_model",
            )
        },
        "repair_source": {
            "report": str(baseline_path),
            "report_sha256": baseline_digest,
            "max_rounds": 2,
        },
    }
    source_report_path = source_root / "report.json"
    source_report_path.write_text(json.dumps(source_report))
    source_ledger_path = source_root / "repair-ledger.json"
    source_ledger_path.write_text(
        json.dumps(
            {
                "source_report": str(baseline_path),
                "source_report_sha256": baseline_digest,
                "entries": entries,
            }
        )
    )
    source_report_digest = sha256(source_report_path.read_bytes()).hexdigest()
    source_ledger_digest = sha256(source_ledger_path.read_bytes()).hexdigest()

    settings = SimpleNamespace(
        workflow=SimpleNamespace(
            enabled=True,
            planner="planner",
            coder="coder",
            reviewer="reviewer",
            premium_reviewer="premium",
        ),
        profiles={
            name: SimpleNamespace(model=model)
            for name, model in {
                "planner": "deepseek-flash",
                "coder": "deepseek-flash",
                "reviewer": "deepseek-flash",
                "premium": "kimi-k3",
            }.items()
        },
    )
    repaired = {
        **baseline_row,
        "passed": True,
        "failure_checks": [],
        "usage": {"total_tokens": 20},
    }
    resumed_entries = [
        {
            **entries[0],
            "rounds": [repaired],
            "superseded_transport_attempts": [failed],
            "final_passed": True,
            "stop_reason": "passed",
        }
    ]
    monkeypatch.setattr(compare_modes, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(
        compare_modes,
        "resume_repair_transport",
        lambda _baseline, _entries, _args: resumed_entries,
    )
    output = tmp_path / "repair-resumed"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_modes",
            "--live",
            "--output",
            str(output),
            "--resume-repair-from",
            str(source_root),
            "--workers",
            "1",
        ],
    )

    assert compare_modes.main() == 0
    report = json.loads((output / "report.json").read_text())
    assert report["repair"]["post_repair"]["strict_successes"] == 1
    assert report["repair_continuation"]["source_report_sha256"] == source_report_digest
    assert report["repair_continuation"]["source_ledger_sha256"] == source_ledger_digest
    assert sha256(source_report_path.read_bytes()).hexdigest() == source_report_digest
    assert sha256(source_ledger_path.read_bytes()).hexdigest() == source_ledger_digest


def test_repair_continuation_rejects_ledger_from_another_baseline_before_loading_models(
    tmp_path,
):
    """账本与修复报告的基线指纹不一致时，不能进入配置加载或产生付费请求。"""
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline_path = baseline_root / "report.json"
    baseline_path.write_text(json.dumps({"trials": []}))
    baseline_digest = sha256(baseline_path.read_bytes()).hexdigest()

    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "report.json").write_text(
        json.dumps(
            {
                "grader_version": compare_modes.GRADER_VERSION,
                "repair_source": {
                    "report": str(baseline_path),
                    "report_sha256": baseline_digest,
                    "max_rounds": 2,
                },
            }
        )
    )
    (source_root / "repair-ledger.json").write_text(
        json.dumps(
            {
                "source_report": str(baseline_path),
                "source_report_sha256": "0" * 64,
                "entries": [],
            }
        )
    )

    with pytest.raises(ValueError, match="账本.*基线"):
        compare_modes.run_repair_continuation(
            source_root,
            SimpleNamespace(
                config=tmp_path / "missing.toml",
                output=tmp_path / "output",
            ),
        )

    assert not (tmp_path / "output").exists()
