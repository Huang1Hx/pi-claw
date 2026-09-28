"""三种 Agent 架构对照评测的评分、核算与可靠性公式。"""

import json
import subprocess
import sys

import pytest

from core.schemas import ToolCall
from evaluation.compare_modes import (
    VERIFY_ARGV,
    approval_for,
    build_public_summary,
    build_trial_report,
    combine_resumed_reports,
    reliability,
    select_resume_jobs,
    summarize,
)
from evaluation.mode_grading import calibrate, safe_functions
from evaluation.mode_tasks import TASKS
from persistence.trace import TraceWriter

STAGE2_TASK_IDS = {
    "balance_transfer",
    "config_overlay",
    "dependency_order",
    "event_deduplication",
    "inventory_reservation",
    "permission_filter",
    "rate_limit_window",
    "versioned_update",
    "window_merge",
    "worker_assignment",
}


def _trial(tmp_path, *, mode="single"):
    """创建一个无需模型的成功 trial，便于测试外部评分而非模型输出。"""
    folder = tmp_path / mode
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
                "subagents": [] if mode != "team" else [{}, {}],
                "usage": {
                    "prompt_tokens": 70,
                    "completion_tokens": 30,
                    "total_tokens": 100,
                    "requests": 2,
                    "requests_with_usage": 2,
                    "requests_without_usage": 0,
                    "retries": 0,
                    "unknown_participants": 0,
                },
                "duration_seconds": 1.25,
                "max_parallel_subagents": 2 if mode == "team" else 0,
            }
        )
    )
    leader = TraceWriter(folder / "trace-leader.jsonl")
    leader.record("run_started", {"task": task.prompt})
    leader.record(
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
    leader.record("run_completed", {"final_text": "done"})
    if mode == "team":
        for name in ("implementation", "risks"):
            child = TraceWriter(folder / f"trace-subagent-{name}.jsonl")
            child.record("run_started", {"task": name})
            child.record("run_completed", {"final_text": "finding"})
    return folder


def test_grader_calibration_and_exact_signatures():
    """参考实现必须全过，候选不能借改签名或危险默认值绕开协议。"""
    assert calibrate()["passed"] is True
    with pytest.raises(ValueError, match="函数签名"):
        safe_functions("def reserve(stock, orders=None):\n    return False, {}\n", "reserve")


def test_grader_calibration_is_repeatable_and_does_not_mutate_hidden_cases():
    """缺陷实现即使修改输入，也不能污染下一次校准或后续真实 trial。"""
    first = calibrate()
    second = calibrate()
    assert first == second
    assert second["passed"] is True


def test_stage2_catalog_has_ten_calibrated_medium_tasks():
    """任务缺失、签名漂移或初始缺陷逃过隐藏评分时，真实基线不具备比较意义。"""
    assert set(TASKS) == STAGE2_TASK_IDS
    assert all(task.parameters and len(task.parameters) <= 4 for task in TASKS.values())
    calibration = calibrate()
    assert calibration["passed"] is True
    assert set(calibration["tasks"]) == STAGE2_TASK_IDS
    assert all(row["passed"] is True for row in calibration["tasks"].values())


@pytest.mark.parametrize("task_id", sorted(STAGE2_TASK_IDS))
def test_each_reference_implementation_passes_its_public_suite(tmp_path, task_id):
    """隐藏评分参考答案也必须通过 Agent 实际运行的公开 unittest。"""
    task = TASKS[task_id]
    workspace = tmp_path / task_id
    workspace.mkdir()
    for name, content in task.files.items():
        (workspace / name).write_text(content)
    (workspace / "solution.py").write_text(task.reference)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "-q", "test_target.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bounded_candidate_allows_safe_non_recursive_helper(tmp_path):
    """本地纯辅助函数不能再导致公开测试审批被误拒，递归辅助函数仍拒绝。"""
    task = TASKS["inventory_reservation"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in task.files.items():
        (workspace / name).write_text(content)
    helper_source = (
        task.reference.replace(
            "def reserve(stock, orders):",
            "def _valid_int(value):\n"
            "    return isinstance(value, int) and not isinstance(value, bool)\n\n\n"
            "def reserve(stock, orders):",
        )
        .replace(
            "not isinstance(quantity, int)\n                or isinstance(quantity, bool)",
            "not _valid_int(quantity)",
        )
        .replace(
            "not isinstance(quantity, int)\n                or isinstance(quantity, bool)",
            "not _valid_int(quantity)",
        )
    )
    (workspace / "solution.py").write_text(helper_source)
    assert safe_functions(helper_source, "reserve")["reserve"]({"a": 2}, [("a", 1)]) == (
        True,
        {"a": 1},
    )
    approve = approval_for("inventory_reservation", workspace)
    assert approve(ToolCall("verify", "run_command", {"argv": VERIFY_ARGV}), None)
    recursive = """def helper(value):
    return helper(value)

def reserve(stock, orders):
    return helper(stock)
"""
    with pytest.raises(ValueError, match="递归"):
        safe_functions(recursive, "reserve")


def test_bounded_candidate_allows_safe_sort_key_but_not_reverse_keyword(tmp_path):
    """常见局部排序写法可验证；仍拒绝评分协议没有开放的其他关键字参数。"""
    task = TASKS["window_merge"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in task.files.items():
        (workspace / name).write_text(content)
    source = task.reference.replace(
        "for start, end in sorted(cleaned):",
        "cleaned.sort(key=lambda item: (item[0], item[1]))\n    for start, end in cleaned:",
    )
    (workspace / "solution.py").write_text(source)
    assert safe_functions(source, "merge_windows")["merge_windows"]([(2, 4), (1, 3)]) == [(1, 4)]
    approve = approval_for("window_merge", workspace)
    assert approve(ToolCall("verify", "run_command", {"argv": VERIFY_ARGV}), None)
    with pytest.raises(ValueError, match="函数调用"):
        safe_functions(
            source.replace("key=lambda item: (item[0], item[1])", "reverse=True"), "merge_windows"
        )


@pytest.mark.parametrize("mode", ["single", "workflow", "team"])
def test_strict_trial_report_uses_same_external_grader(tmp_path, mode):
    """三种架构共用最终文件、公开测试、隐藏用例与 Trace 完整性门槛。"""
    folder = _trial(tmp_path, mode=mode)
    report = build_trial_report(
        folder,
        {"mode": mode, "task": "inventory_reservation", "trial": 1, "duration_seconds": 9},
    )
    assert report["passed"] is True
    assert report["duration_seconds"] == 1.25
    assert report["participants"] == (3 if mode == "team" else 1)


def test_unauthorized_attempt_is_deduplicated_by_participant_and_call_id(tmp_path):
    """同一子 Agent 调用在 model_reply 和 tool_result 出现时只计一次。"""
    folder = _trial(tmp_path, mode="team")
    path = folder / "trace-subagent-risks.jsonl"
    path.unlink()
    trace = TraceWriter(path)
    call = {"id": "write-1", "name": "write_or_replace", "arguments": {"path": "solution.py"}}
    trace.record("model_reply", {"tool_call": call})
    trace.record(
        "tool_result",
        {
            "call_id": "write-1",
            "tool_name": "write_or_replace",
            "ok": False,
            "output": None,
            "error": "策略拒绝",
            "metadata": {"policy": {"action": "deny"}},
        },
    )
    report = build_trial_report(
        folder,
        {"mode": "team", "task": "inventory_reservation", "trial": 1, "duration_seconds": 9},
    )
    assert report["unauthorized_attempts"] == 1


def test_reliability_and_token_per_success_include_failed_trials():
    """pass@k/pass^k 按任务计算；每成功 Token 的分子包含失败与返工消耗。"""
    rows = [
        {
            "task": "a",
            "passed": True,
            "duration_seconds": 1,
            "usage": {"total_tokens": 100},
            "usage_may_be_incomplete": False,
            "models_used": ["m"],
            "tool_calls": 1,
            "unauthorized_attempts": 0,
            "write_conflicts": 0,
            "rework_events": 0,
            "max_parallel_subagents": 0,
        },
        {
            "task": "a",
            "passed": False,
            "duration_seconds": 3,
            "usage": {"total_tokens": 300},
            "usage_may_be_incomplete": False,
            "models_used": ["m"],
            "tool_calls": 2,
            "unauthorized_attempts": 1,
            "write_conflicts": 0,
            "rework_events": 1,
            "max_parallel_subagents": 0,
        },
    ]
    assert reliability(rows)["observed_pass_at_2"] == 1
    assert reliability(rows)["observed_pass_power_2"] == 0
    summary = summarize(rows)
    assert summary["known_total_tokens"] == 400
    assert summary["known_tokens_per_strict_success"] == 400
    assert summary["median_duration_seconds"] == 2


def test_summary_reports_tail_latency_timeouts_status_layers_and_model_requests():
    """固定样本锁定 P95、失败分层和高级模型触发次数，避免漂亮均值掩盖长尾。"""
    rows = []
    for duration, status, passed, layer, requests in (
        (1, "completed", True, None, {"deepseek-flash": 2}),
        (2, "completed", False, "L2_semantics", {"deepseek-flash": 3}),
        (3, "deadline", False, "L0_execution", {"deepseek-flash": 1, "kimi-k3": 1}),
        (100, "harness_error", False, "L0_execution", {}),
    ):
        rows.append(
            {
                "task": "a",
                "passed": passed,
                "leader_status": status,
                "duration_seconds": duration,
                "usage": {"total_tokens": 10},
                "usage_may_be_incomplete": status == "deadline",
                "models_used": list(requests),
                "model_requests": requests,
                "tool_calls": 0,
                "unauthorized_attempts": 0,
                "write_conflicts": 0,
                "rework_events": 0,
                "max_parallel_subagents": 0,
                "failure_checks": []
                if layer is None
                else [{"name": "failed", "passed": False, "layer": layer}],
            }
        )
    summary = summarize(rows, premium_model="kimi-k3")
    assert summary["strict_successes"] == 1
    assert summary["p50_duration_seconds"] == 2.5
    assert summary["p95_duration_seconds"] == 100
    assert summary["timeout_trials"] == 1
    assert summary["timeout_rate"] == 0.25
    assert summary["status_distribution"] == {
        "completed": 2,
        "deadline": 1,
        "harness_error": 1,
    }
    assert summary["failure_layer_distribution"] == {"L0_execution": 2, "L2_semantics": 1}
    assert summary["model_request_distribution"] == {"deepseek-flash": 6, "kimi-k3": 1}
    assert summary["premium_model_requests"] == 1


def test_per_model_usage_separates_known_tokens_from_missing_premium_usage(tmp_path):
    """Kimi 已知 Token 与路由后无 usage 的请求必须分别记录，不能把缺口当作零。"""
    folder = _trial(tmp_path)
    result_path = folder / "run-result.json"
    result = json.loads(result_path.read_text())
    result["leader"]["routing"] = {
        "models": {
            "premium_reviewer": {
                "requests": 1,
                "prompt_tokens": 10,
                "completion_tokens": 2,
            }
        }
    }
    result_path.write_text(json.dumps(result))
    trace = TraceWriter(folder / "trace-leader.jsonl")
    for request in (1, 2):
        trace.record(
            "model_routed",
            {
                "profile": "premium_reviewer",
                "model": "kimi-k3",
                "purpose": "premium_reviewing",
                "request": request,
            },
        )
    report = build_trial_report(
        folder,
        {"mode": "single", "task": "inventory_reservation", "trial": 1, "duration_seconds": 9},
    )
    assert report["model_usage"]["kimi-k3"] == {
        "attempted_requests": 2,
        "requests_with_usage": 1,
        "requests_without_usage": 1,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    summary = summarize([report], premium_model="kimi-k3")
    assert summary["premium_model_usage"] == report["model_usage"]["kimi-k3"]


def test_public_summary_excludes_trial_paths_traces_and_connection_data():
    """可提交摘要只保留白名单聚合值，不泄漏本机目录、Trace 或连接配置。"""
    report = {
        "schema_version": 2,
        "grader_version": "test",
        "generated_at": "2026-09-27T00:00:00Z",
        "real_model": True,
        "network_used": True,
        "source_sha256": "abc",
        "modes": ["single"],
        "tasks": ["a"],
        "trials_per_task": 3,
        "workers": 2,
        "deadline_seconds": 420,
        "max_steps": 24,
        "models": {"coder": "deepseek-flash"},
        "premium_model": "kimi-k3",
        "accounting": "all participants",
        "by_mode": {"single": {"strict_successes": 1}},
        "by_mode_and_task": {"single": {"a": {"strict_successes": 1}}},
        "trials": [
            {
                "workspace": "/Users/example/private",
                "traces": ["/Users/example/trace.jsonl"],
                "api_key": "secret-value",
                "base_url": "https://private.invalid",
            }
        ],
        "config_path": "/Users/example/pi-claw.toml",
    }
    public = build_public_summary(report)
    text = json.dumps(public)
    assert public["by_mode_and_task"]["single"]["a"]["strict_successes"] == 1
    assert "trials" not in public
    assert "config_path" not in public
    assert "/Users/" not in text
    assert "trace.jsonl" not in text
    assert "secret-value" not in text
    assert "private.invalid" not in text


def test_resume_jobs_select_only_provider_errors_and_preserve_trial_identity():
    """恢复批次只能选择传输失败项，不能把语义失败偷偷重跑成更好结果。"""
    report = {
        "trials": [
            {
                "mode": "single",
                "task": "worker_assignment",
                "trial": 2,
                "leader_status": "provider_error",
            },
            {
                "mode": "workflow",
                "task": "worker_assignment",
                "trial": 2,
                "leader_status": "completed",
            },
            {
                "mode": "team",
                "task": "versioned_update",
                "trial": 3,
                "leader_status": "loop_detected",
            },
        ]
    }
    assert select_resume_jobs(report) == [("single", "worker_assignment", 2)]


@pytest.mark.parametrize(
    "row",
    [
        {"mode": "../../escape", "task": "worker_assignment", "trial": 1},
        {"mode": "single", "task": "unknown", "trial": 1},
        {"mode": "single", "task": "worker_assignment", "trial": "1"},
    ],
)
def test_resume_rejects_untrusted_trial_identity(row):
    """损坏或伪造的来源报告不能借 mode/task 字段把恢复工作区写到输出目录之外。"""
    with pytest.raises(ValueError, match="trial 身份"):
        select_resume_jobs({"trials": [{**row, "leader_status": "provider_error"}]})


def test_resume_replaces_provider_error_and_regrades_retained_trial(tmp_path):
    """合并报告使用新结果替换传输失败，同时将旧有效 trial 提升到当前评分器口径。"""
    retained_folder = _trial(tmp_path / "retained")
    failed_folder = _trial(tmp_path / "failed")
    retained = build_trial_report(
        retained_folder,
        {
            "mode": "single",
            "task": "inventory_reservation",
            "trial": 1,
            "duration_seconds": 1,
        },
    )
    failed = build_trial_report(
        failed_folder,
        {
            "mode": "single",
            "task": "inventory_reservation",
            "trial": 2,
            "duration_seconds": 1,
        },
    )
    failed["leader_status"] = "provider_error"
    replacement = {**failed, "leader_status": "completed", "passed": True}

    combined = combine_resumed_reports(
        {"trials": [retained, failed]},
        [replacement],
    )

    assert [(row["trial"], row["leader_status"]) for row in combined] == [
        (1, "completed"),
        (2, "completed"),
    ]
    assert all(row["grader_version"] == "agent-modes-2.1" for row in combined)


def test_deadline_recovers_known_partial_usage_from_all_traces(tmp_path):
    """无完整结果时保留已完成请求 Token，同时显式标成不完整而不是填成零。"""
    folder = _trial(tmp_path, mode="team")
    result = json.loads((folder / "run-result.json").read_text())
    result["leader"] = {"status": "deadline"}
    result["usage"] = None
    (folder / "run-result.json").write_text(json.dumps(result))
    for index, name in enumerate(("leader", "subagent-implementation", "subagent-risks"), 1):
        TraceWriter(folder / f"trace-{name}.jsonl").record(
            "usage_updated",
            {
                "prompt_tokens": index * 10,
                "completion_tokens": index,
                "total_tokens": index * 11,
                "requests": 1,
                "requests_with_usage": 1,
                "requests_without_usage": 0,
                "retries": 0,
            },
        )
    report = build_trial_report(
        folder,
        {"mode": "team", "task": "inventory_reservation", "trial": 1, "duration_seconds": 420},
    )
    assert report["usage_source"] == "partial_trace"
    assert report["usage"]["total_tokens"] == 66
    assert report["usage_may_be_incomplete"] is True
    assert report["participants"] == 3


def test_timed_out_logical_request_is_an_explicit_usage_gap(tmp_path):
    """路由已发起但没有完整响应的请求不能因为缺少 usage 被当成零消耗。"""
    folder = _trial(tmp_path)
    result = json.loads((folder / "run-result.json").read_text())
    result["leader"]["routing"] = {"requests": 3}
    result["leader"]["usage"] = dict(result["usage"])
    result["usage"]["requests"] = 2
    result["usage"]["requests_with_usage"] = 2
    (folder / "run-result.json").write_text(json.dumps(result))
    report = build_trial_report(
        folder,
        {"mode": "single", "task": "inventory_reservation", "trial": 1, "duration_seconds": 9},
    )
    assert report["usage"]["attempted_requests"] == 3
    assert report["usage"]["requests_without_usage"] == 1
    assert report["usage_may_be_incomplete"] is True
