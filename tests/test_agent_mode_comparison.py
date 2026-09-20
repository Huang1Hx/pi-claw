"""三种 Agent 架构对照评测的评分、核算与可靠性公式。"""

import json

import pytest

from evaluation.compare_modes import VERIFY_ARGV, build_trial_report, reliability, summarize
from evaluation.mode_grading import calibrate, safe_functions
from evaluation.mode_tasks import TASKS
from persistence.trace import TraceWriter


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
