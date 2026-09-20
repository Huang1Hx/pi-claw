"""量化必须连同分母、缺失证据和 Trace 定位一起测试；本文件不访问任何模型。"""

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from core.schemas import ToolCall
from evaluation import live
from evaluation.grading import check, diagnosis, grade
from evaluation.report import build_report, read_trace, scores, summarize
from evaluation.tasks import INITIAL, READ_EXPECTED, REFERENCE, VISIBLE_TEST


def make_trial(folder, *, task="inspect_auth", final=None, status="completed", source=INITIAL):
    """构造最小可观察产物，严格区分替身证据和真实模型成绩。"""
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "auth.py").write_text(source)
    (workspace / "test_auth.py").write_text(VISIBLE_TEST)
    (folder / "agent-result.json").write_text(
        json.dumps(
            {
                "status": status,
                "final_text": json.dumps(READ_EXPECTED) if final is None else final,
                "usage": {"total_tokens": 100},
            }
        )
    )
    event = {"run_id": "test-run", "step": 1, "event": "model_reply", "payload": {}}
    (folder / "trace.jsonl").write_text(json.dumps(event) + "\n")
    return {"task": task, "trial": 1, "route": "primary", "duration_seconds": 1.5}


@pytest.mark.parametrize(
    "prefix,suffix,strict",
    [
        ("", "", True),
        ("```json\n", "\n```", True),
        ("```json\n", "\n```\n补充说明", False),
        ("答案：", "", False),
    ],
)
def test_semantics_are_independent_of_format(prefix, suffix, strict):
    """格式多余只扣契约分，不把四个正确事实判成四次知识错误。"""
    answer, actual_strict = diagnosis(prefix + json.dumps(READ_EXPECTED) + suffix)
    assert answer == READ_EXPECTED
    assert actual_strict == strict


@pytest.mark.parametrize(
    "answer",
    [
        '{"x": true, "x": false}',
        '{"x": true}\n{"x": false}',
        '{"broken":',
        "没有结构化答案",
    ],
)
def test_ambiguous_or_malformed_answer_is_not_cherry_picked(answer):
    """重复键、多个答案和非法 JSON 都不采用最后一个或最有利的值。"""
    assert diagnosis(answer) == (None, False)


def test_format_failure_has_full_semantic_score_and_exact_trace(tmp_path):
    """正确诊断但额外 prose：语义 100，契约 50（JSON 失败、completed 通过）。"""
    meta = make_trial(tmp_path, final=json.dumps(READ_EXPECTED) + "\n补充")
    report = build_report(tmp_path, meta)
    assert not report["passed"]
    assert report["layers"]["L2_semantics"]["score_percent"] == 100
    assert report["layers"]["L3_contract"]["score_percent"] == 50
    assert report["completed_with_failed_checks"]
    assert not report["completed_but_wrong"]
    (failure,) = report["failures"]
    ref = failure["evidence"][0]
    assert (ref["line"], ref["step"], ref["run_id"]) == (1, 1, "test-run")
    assert Path(ref["path"]).is_file()


def test_timeout_keeps_denominator_and_marks_missing_semantics(tmp_path):
    """没有最终回答不是四条事实都错；端到端仍失败，Token 也不能认作零消费。"""
    meta = make_trial(tmp_path, final="", status="provider_error")
    report = build_report(tmp_path, meta)
    assert not report["passed"]
    assert report["failure_category"] == "L0_execution"
    assert report["layers"]["L2_semantics"]["score_percent"] is None
    assert report["layers"]["L2_semantics"]["blocked"] == 4
    assert report["usage_may_be_incomplete"]
    summary = summarize([report])
    assert summary["reliability"]["trial_success_rate"] == 0
    assert summary["known_tokens_per_strict_success"] is None
    assert summary["p95_duration_seconds"] is None


def test_invalid_json_is_contract_failure_not_a_factual_error(tmp_path):
    """格式失败造成语义不可评分：优先归因已知失败，而非 blocked 下游。"""
    meta = make_trial(tmp_path, final="没有 JSON")
    report = build_report(tmp_path, meta)
    assert report["failure_category"] == "L3_contract"
    assert not report["completed_but_wrong"]
    assert report["layers"]["L2_semantics"]["blocked"] == 4


def test_truncated_trace_preserves_evidence_and_reports_gap(tmp_path):
    """模拟墙钟超时截断最后一行，仍可查询此前有效行。"""
    meta = make_trial(tmp_path)
    path = tmp_path / "trace.jsonl"
    with path.open("a") as stream:
        stream.write('{"event":')
    events, issues = read_trace(path)
    assert len(events) == 1
    assert issues[0]["line"] == 2
    report = build_report(tmp_path, meta)
    assert not report["passed"]
    failure = next(f for f in report["failures"] if f["check"] == "trace_integrity")
    assert failure["evidence"][0]["line"] == 2


def test_grading_invalid_candidate_blocks_behavior_execution(tmp_path):
    """非法候选不可执行；十个行为项记 blocked 而不是缩小分母隐藏它们。"""
    meta = make_trial(tmp_path, task="fix_auth", source="import os\n" + REFERENCE)
    report = build_report(tmp_path, meta)
    assert report["layers"]["L2_semantics"]["blocked"] == 10
    assert report["behavior_groups"]["fail_to_pass"]["total"] == 4
    assert report["behavior_groups"]["pass_to_pass"]["total"] == 6
    assert report["failure_category"] == "L1_boundary"


def test_good_repair_has_separate_fix_and_regression_scores(tmp_path):
    """新漏洞修复和旧行为保留各自有固定分母。"""
    meta = make_trial(tmp_path, task="fix_auth", source=REFERENCE)
    report = build_report(tmp_path, meta)
    assert report["passed"]
    assert report["behavior_groups"]["fail_to_pass"]["passed"] == 4
    assert report["behavior_groups"]["pass_to_pass"]["passed"] == 6


def test_missing_source_cannot_erase_semantics_denominator(tmp_path):
    """删除目标源码也不能获得空集合的虚假满分。"""
    meta = make_trial(tmp_path, task="fix_auth")
    (tmp_path / "workspace/auth.py").unlink()
    report = build_report(tmp_path, meta)
    assert report["layers"]["L2_semantics"]["blocked"] == 10
    assert not report["passed"]


def test_corrupted_result_is_a_harness_failure(tmp_path):
    """不信任旧 meta 中 completed 声明，更不因一份损坏产物中断其他 trial。"""
    meta = make_trial(tmp_path)
    (tmp_path / "agent-result.json").write_text("{")
    report = build_report(tmp_path, {**meta, "agent_status": "completed"})
    assert report["agent_status"] == "harness_error"
    assert report["result_issue"]


def test_nested_file_with_allowed_basename_is_not_ignored(tmp_path):
    """额外目录内的 auth.py 也属于未授权产物。"""
    make_trial(tmp_path)
    nested = tmp_path / "workspace/other"
    nested.mkdir()
    (nested / "auth.py").write_text(INITIAL)
    checks = grade("inspect_auth", tmp_path / "workspace", json.dumps(READ_EXPECTED), [])
    assert not next(c["passed"] for c in checks if c["name"] == "no_extra_source_files")


def test_score_coverage_is_not_silently_treated_as_accuracy():
    """1 对、1 错、1 缺失：正确率 50%，覆盖率 66.67%，整体不通过。"""
    report = scores([check("a", True), check("b", False), check("c", None)])
    assert report["score_percent"] == 50
    assert report["coverage_percent"] == 66.67
    assert not report["gate_passed"]


def test_route_comparison_does_not_mix_pass_at_k(tmp_path):
    """两模型各一遍仍然都是 k=1，不能伪装成同一模型 k=2。"""
    rows = []
    for route in ("primary", "reviewer"):
        folder = tmp_path / route
        meta = make_trial(folder)
        rows.append(build_report(folder, {**meta, "route": route}))
    result = live.write_batch(tmp_path, {"network_used": False}, rows)
    assert all(r["reliability"]["k"] == 1 for r in result["by_route"].values())
    assert (tmp_path / "failure-index.json").is_file()


def test_regrade_never_calls_worker_or_rewrites_original(tmp_path, monkeypatch):
    """旧报告重评分只产生新目录，未开启 live 时不得访问模型。"""
    original = tmp_path / "old"
    meta = make_trial(original, final=json.dumps(READ_EXPECTED) + "\n补充")
    old_report = original / "report.json"
    old_report.write_text(
        json.dumps(
            {
                "trials": [
                    {
                        **meta,
                        "agent_status": "completed",
                        "workspace": str(original / "workspace"),
                    }
                ]
            }
        )
    )
    before = old_report.read_bytes()
    monkeypatch.setattr(live, "worker", lambda _: pytest.fail("unexpected API worker"))
    monkeypatch.setattr(
        "sys.argv", ["live", "--regrade", str(old_report), "--output", str(tmp_path / "new")]
    )
    assert live.main() == 0
    assert old_report.read_bytes() == before
    new = json.loads((tmp_path / "new/report.json").read_text())
    assert not new["network_used"]
    assert new["trials"][0]["layers"]["L2_semantics"]["score_percent"] == 100


def test_trial_deadline_persists_failure_and_reaps_process_group(tmp_path, monkeypatch):
    """超时后仍产生 grade，且清理精确的 worker 进程组，不让工作在后台继续。"""

    class TimedOutProcess:
        """不启动真实子进程的期限替身。"""

        pid = 12345
        returncode = -9

        def wait(self, timeout=None):
            """第一次到期，清理后的 wait 返回退出码。"""
            if timeout is not None:
                raise subprocess.TimeoutExpired("test worker", timeout)
            return self.returncode

    kills = []
    monkeypatch.setattr(live.subprocess, "Popen", lambda *a, **kw: TimedOutProcess())
    monkeypatch.setattr(live.os, "killpg", lambda pid, sig: kills.append(pid))
    args = Namespace(
        output=tmp_path, config=tmp_path / "unused.toml", route="primary", compare=None, deadline=10
    )
    report = live.run_trial("inspect_auth", 1, args)
    assert kills == [12345]
    assert report["agent_status"] == "deadline"
    assert not report["passed"]
    assert (tmp_path / "inspect_auth-1/grade.json").is_file()


@pytest.mark.parametrize("cwd", [".", "./", "absolute"])
def test_eval_approval_normalizes_equivalent_workspace_cwd(tmp_path, cwd):
    """同一工作区的相对/绝对表示等价，不能因模型选了绝对路径而误拒测试。"""
    make_trial(tmp_path, task="fix_auth", source=REFERENCE)
    workspace = tmp_path / "workspace"
    value = str(workspace.resolve()) if cwd == "absolute" else cwd
    call = ToolCall("verify", "run_command", {"argv": live.VERIFY_ARGV, "cwd": value})
    assert live.approval_for("fix_auth", workspace)(call, None)


@pytest.mark.parametrize("cwd", ["..", "/tmp", "subdir", "missing", None])
def test_eval_approval_still_rejects_other_or_invalid_cwd(tmp_path, cwd):
    """路径等价修正不授予其他目录或缺失/非法参数的执行权限。"""
    make_trial(tmp_path, task="fix_auth", source=REFERENCE)
    workspace = tmp_path / "workspace"
    (workspace / "subdir").mkdir()
    call = ToolCall("verify", "run_command", {"argv": live.VERIFY_ARGV, "cwd": cwd})
    assert not live.approval_for("fix_auth", workspace)(call, None)


def test_recovered_chinese_approval_denial_remains_in_failure_index(tmp_path):
    """整体成功不擦掉中途失败；按生产中文错误识别审批拒绝，不能只匹配英文。"""
    meta = make_trial(tmp_path, task="fix_auth", source=REFERENCE)
    with (tmp_path / "trace.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "run_id": "test-run",
                    "step": 2,
                    "event": "tool_result",
                    "payload": {
                        "call_id": "denied-call",
                        "tool_name": "run_command",
                        "ok": False,
                        "error": "未获批准：逐次批准",
                        "metadata": {"policy": {"action": "ask"}},
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "run_id": "test-run",
                    "step": 3,
                    "event": "completion_rejected",
                    "payload": {"reason": "尚无成功验证"},
                }
            )
            + "\n"
        )
    report = build_report(tmp_path, meta)
    assert report["passed"]
    assert report["denied_tool_calls"] == 1
    assert report["completion_rejections"] == 1
    live.write_batch(tmp_path, {"network_used": False}, [report])
    index = json.loads((tmp_path / "failure-index.json").read_text())
    assert len(index) == 2
    assert all(issue["trial_passed"] and issue["scope"] == "process_event" for issue in index)
    assert index[0]["evidence"][0]["line"] == 2
    assert index[0]["call_id"] == "denied-call"
