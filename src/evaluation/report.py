"""分层量化、失败证据索引与只读复盘；不需要模型、Key 或私有推理内容。"""

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from statistics import median

from evaluation.grading import GRADER_VERSION, check, grade, reliability
from evaluation.tasks import CASES, READ_EXPECTED
from persistence.trace import redact

LAYERS = ("L0_execution", "L1_boundary", "L2_semantics", "L3_contract")


def read_trace(path: Path) -> tuple[list[dict], list[dict]]:
    """保留损坏/超时 Trace 的有效行；显式报告缺口，不让一个坏 trial 中断整批。"""
    if not path.is_file():
        return [], [{"line": None, "error": "trace missing"}]
    if path.stat().st_size > 16 * 1024 * 1024:
        return [], [{"line": None, "error": "trace exceeds 16 MiB limit"}]
    events, issues = [], []
    for line, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                raise ValueError("invalid event object")
            if not isinstance(event.get("event"), str):
                raise ValueError("missing event name")
            events.append({**event, "_line": line})
        except ValueError as exc:
            issues.append({"line": line, "error": str(exc)})
    if not events and not issues:
        issues.append({"line": None, "error": "empty trace"})
    return events, issues


def scores(checks: list[dict]) -> dict:
    """分母为已评分项，另报覆盖率；缺失项不计正确，但严格 gate 不能通过。"""
    passed = sum(c["passed"] is True for c in checks)
    evaluated = sum(c["passed"] is not None for c in checks)
    total = len(checks)
    return {
        "passed": passed,
        "evaluated": evaluated,
        "total": total,
        "blocked": total - evaluated,
        "score_percent": round(100 * passed / evaluated, 2) if evaluated else None,
        "coverage_percent": round(100 * evaluated / total, 2) if total else None,
        "gate_passed": all(c["passed"] is True for c in checks) if total else None,
    }


def evidence_for(item: dict, folder: Path, events: list[dict]) -> list[dict]:
    """定位可观察证据，不将最后一次写入误称为因果根源；行号为 JSONL 的物理行号。"""
    name = item["name"]
    names = (
        {"provider_error"}
        if name == "transport_ok"
        else {"tool_call", "tool_result"}
        if name == "no_side_effect_attempt"
        else {"model_reply", "completion_rejected", "completion_verified"}
        if name in {"diagnosis_json", "agent_completed"} or name in READ_EXPECTED
        else {"tool_result"}
    )
    related = [e for e in events if e["event"] in names]
    if item["passed"] is None and not related:
        related = [e for e in events if e["event"] == "provider_error"]
    # 只读副作用失败应直接展示相应调用，而不是最后一条无关 read_file。
    if name == "no_side_effect_attempt":
        related = [
            e
            for e in related
            if e["payload"].get("name", e["payload"].get("tool_name"))
            in {"write_or_replace", "apply_patch", "run_command"}
        ]
    refs = [
        {
            "kind": "related_trace_event",
            "path": str((folder / "trace.jsonl").resolve()),
            "line": e["_line"],
            "run_id": e.get("run_id"),
            "step": e.get("step"),
            "event": e["event"],
            "call_id": e["payload"].get("call_id", e["payload"].get("id")),
        }
        for e in related[-3:]
    ]
    # 原始文件独立于模型措辞及 Trace 的脱敏/截断，用于复核最终状态。
    artifact = (
        "agent-result.json"
        if item["layer"] == "L3_contract" or name in READ_EXPECTED
        else "workspace/auth.py"
    )
    if name == "grader_fixture_unchanged":
        artifact = "workspace/test_auth.py"
    if item["layer"] == "L0_execution":
        artifact = "progress.log"
    path = folder / artifact
    refs.append({"kind": "artifact", "path": str(path.resolve()), "exists": path.is_file()})
    return refs


def build_report(folder: Path, metadata: dict) -> dict:
    """只读最终产物重新评分；旧版成绩可在新目录复核，不回写历史证据。"""
    result_path = folder / "agent-result.json"
    result_issue = None
    try:
        result = json.loads(result_path.read_text())
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise ValueError("invalid result object")
    except (OSError, ValueError) as exc:
        result_issue = f"{type(exc).__name__}: unreadable agent result"
        result = {"status": "harness_error", "final_text": ""}
    events, issues = read_trace(folder / "trace.jsonl")
    status = result["status"]
    outcomes = grade(metadata["task"], folder / "workspace", result.get("final_text") or "", events)
    checks = [
        check(
            "transport_ok", status not in {"deadline", "provider_error", "harness_error"}, status
        ),
        check(
            "trace_integrity", not issues, json.dumps(issues, ensure_ascii=False) if issues else ""
        ),
        *outcomes,
        check("agent_completed", status == "completed", status),
    ]
    layers = {layer: scores([c for c in checks if c["layer"] == layer]) for layer in LAYERS}
    groups = (
        {
            group: scores(
                [c for c in checks if c["name"] in {case[0] for case in CASES if case[1] == group}]
            )
            for group in ("fail_to_pass", "pass_to_pass")
        }
        if metadata["task"] == "fix_auth"
        else {}
    )
    usage = result.get("usage")
    if usage is None:
        recorded = [e["payload"] for e in events if e["event"] == "usage_updated"]
        usage = recorded[-1] if recorded else None
    tool_results = [e["payload"] for e in events if e["event"] == "tool_result"]
    denied = [
        t
        for t in tool_results
        if not t.get("ok")
        and (
            t.get("metadata", {}).get("policy", {}).get("action") == "deny"
            or (t.get("error") or "").startswith("未获批准：")
        )
    ]
    passed = all(c["passed"] is True for c in checks)
    failures = [
        {
            "check": c["name"],
            "layer": c["layer"],
            "state": "blocked" if c["passed"] is None else "failed",
            "detail": c["detail"],
            "evidence": evidence_for(c, folder, events),
        }
        for c in checks
        if c["passed"] is not True
    ]
    for failure in failures:
        if failure["check"] == "trace_integrity":
            failure["evidence"] = [
                {
                    "kind": "trace_gap",
                    "path": str((folder / "trace.jsonl").resolve()),
                    **issue,
                }
                for issue in issues
            ]
    semantic_failure = any(c["passed"] is False and c["layer"] == "L2_semantics" for c in checks)
    return {
        **metadata,
        "grader_version": GRADER_VERSION,
        "passed": passed,
        "outcome_passed": all(c["passed"] is True for c in outcomes),
        "agent_status": status,
        "failure_category": next(
            (
                layer
                for layer in LAYERS
                if any(c["layer"] == layer and c["passed"] is False for c in checks)
            ),
            next((layer for layer in LAYERS if layers[layer]["gate_passed"] is False), "passed"),
        ),
        "completed_with_failed_checks": status == "completed" and not passed,
        "completed_but_wrong": status == "completed" and semantic_failure,
        "checks": checks,
        "layers": layers,
        "behavior_groups": groups,
        "passed_checks": sum(c["passed"] is True for c in checks),
        "total_checks": len(checks),
        "failures": failures,
        "trace_issues": issues,
        "result_issue": result_issue,
        "usage": usage,
        "usage_may_be_incomplete": status in {"deadline", "provider_error", "harness_error"}
        or usage is None
        or bool(usage.get("requests_without_usage")),
        "tool_calls": len(tool_results),
        "non_ok_tools": sum(not t.get("ok") for t in tool_results),
        "denied_tool_calls": len(denied),
        "completion_rejections": sum(e["event"] == "completion_rejected" for e in events),
        "process_issues": [
            {
                "event": e["event"],
                "step": e.get("step"),
                "run_id": e.get("run_id"),
                "call_id": e["payload"].get("call_id"),
                "tool_name": e["payload"].get("tool_name"),
                "detail": e["payload"].get("error") or e["payload"].get("reason"),
                "evidence": [
                    {
                        "kind": "trace_event",
                        "path": str((folder / "trace.jsonl").resolve()),
                        "line": e["_line"],
                    }
                ],
            }
            for e in events
            if e["event"] == "completion_rejected"
            or (e["event"] == "tool_result" and e["payload"].get("ok") is False)
        ],
        "first_nonempty_text_seconds": result.get("first_nonempty_text_seconds"),
        "routing": result.get("routing"),
        "models_used": sorted(
            {
                e["payload"]["model"]
                for e in events
                if e["event"] == "model_routed" and isinstance(e["payload"].get("model"), str)
            }
        ),
        "trace": str((folder / "trace.jsonl").resolve()),
        "workspace": str((folder / "workspace").resolve()),
        "artifact_sha256": {
            p.name: sha256(p.read_bytes()).hexdigest()
            for p in (folder / "workspace/auth.py", folder / "workspace/test_auth.py")
            if p.is_file()
        },
        "final_text": result.get("final_text"),
    }


def summarize(reports: list[dict]) -> dict:
    """每个 route 单独算重复可靠性；不把不同模型的两次运行凑成 pass@2。"""
    checks = [c for r in reports for c in r["checks"]]
    known_tokens = sum((r["usage"] or {}).get("total_tokens", 0) for r in reports)
    success = sum(r["passed"] for r in reports)
    unknown = sum(r["usage_may_be_incomplete"] for r in reports)
    durations = [r["duration_seconds"] for r in reports]
    return {
        "models_used": sorted({model for r in reports for model in r["models_used"]}),
        "reliability": reliability(reports),
        "layers": {layer: scores([c for c in checks if c["layer"] == layer]) for layer in LAYERS},
        "behavior_groups": {
            group: scores(
                [c for c in checks if c["name"] in {case[0] for case in CASES if case[1] == group}]
            )
            for group in ("fail_to_pass", "pass_to_pass")
        },
        "failure_categories": dict(
            Counter(r["failure_category"] for r in reports if not r["passed"])
        ),
        "known_tokens": known_tokens,
        "unknown_usage_trials": unknown,
        "known_tokens_per_strict_success": round(known_tokens / success, 2) if success else None,
        "tokens_per_success_is_lower_bound": bool(unknown),
        "completed_with_failed_checks": sum(r["completed_with_failed_checks"] for r in reports),
        "completed_but_wrong": sum(r["completed_but_wrong"] for r in reports),
        "tool_calls": sum(r["tool_calls"] for r in reports),
        "denied_tool_calls": sum(r["denied_tool_calls"] for r in reports),
        "non_ok_tools": sum(r["non_ok_tools"] for r in reports),
        "completion_rejections": sum(r["completion_rejections"] for r in reports),
        "median_duration_seconds": round(median(durations), 2),
        "max_duration_seconds": max(durations),
        "p95_duration_seconds": sorted(durations)[-(-95 * len(durations) // 100) - 1]
        if len(durations) >= 20
        else None,
        "p95_minimum_samples": 20,
    }


def main() -> int:
    """输入 trial 目录打印分层结果与失败定位，不重放工具、不调用模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    args = parser.parse_args()
    report = json.loads((args.trial / "grade.json").read_text())
    print(
        json.dumps(
            redact(
                {
                    key: report.get(key)
                    for key in (
                        "task",
                        "trial",
                        "route",
                        "grader_version",
                        "agent_status",
                        "passed",
                        "layers",
                        "failures",
                        "process_issues",
                    )
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
