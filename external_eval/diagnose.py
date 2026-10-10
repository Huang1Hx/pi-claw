"""坏例诊断：从报告与 Trace 还原失败原因，并给出可执行的自动修复策略。

- classify：把每个实例归入一个失败大类，并附证据（步数、补丁大小、失败用例、Trace 终止事件）；
- failing_tests_from_junit：从容器/本地 junit.xml 提取"哪些目标用例失败、报错首行"；
- plan_retry：按失败大类给出重试参数（步数、是否从 checkpoint 续跑、纠正提示）。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

# 失败大类：按"先环境、再运行、后模型产出"的顺序判定。
SOLVED = "solved"
BUILD_ERROR = "build_error"
RUN_ERROR = "run_error"
MAX_STEPS = "max_steps"
LOOP_DETECTED = "loop_detected"
EMPTY_PATCH = "empty_patch"
TARGET_TESTS_FAILED = "target_tests_failed"
REGRESSION_ONLY = "regression_only"

# 哪些原因值得自动重试（其余属环境问题，重试无意义）。
RETRYABLE = {
    RUN_ERROR,
    MAX_STEPS,
    LOOP_DETECTED,
    EMPTY_PATCH,
    TARGET_TESTS_FAILED,
    REGRESSION_ONLY,
}


def load_trace_events(trace_path: Path) -> list[dict]:
    if not trace_path.is_file():
        return []
    events = []
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def failing_tests_from_junit(inst_dir: Path, wanted_ids: list[str]) -> list[dict]:
    """从 junit.xml 提取目标用例中失败的条目及报错首行。"""
    junit = Path(inst_dir) / "junit.xml"
    if not junit.is_file():
        return []
    try:
        root = ET.fromstring(junit.read_text())
    except ET.ParseError:
        return []
    repo_dir = Path(inst_dir) / "repo"
    results: list[dict] = []
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        parts = classname.split(".") if classname else []
        nodeid = None
        for k in range(len(parts), 0, -1):
            candidate = "/".join(parts[:k]) + ".py"
            if (repo_dir / candidate).is_file():
                nodeid = candidate + "::" + "::".join(parts[k:] + [name])
                break
        if nodeid is None or nodeid not in wanted_ids:
            continue
        bad = [child for child in case if child.tag in {"failure", "error"}]
        if not bad:
            continue
        message = (bad[0].get("message") or "").strip().splitlines()
        results.append({"test": nodeid, "message": (message[0] if message else "")[:300]})
    return results


def classify(record: dict, *, root: Path) -> dict:
    """把 batch/sweep 记录 + 报告 + Trace 归成一个失败大类。"""
    iid = record["instance_id"]
    inst_dir = Path(root) / iid
    out = {"instance_id": iid, "repo": record.get("repo")}
    if record.get("relative_resolved"):
        out.update({"reason": SOLVED, "evidence": ""})
        return out
    if record.get("error"):
        out.update({"reason": BUILD_ERROR, "evidence": record["error"][:300]})
        return out

    report_path = Path(root).parent / "report" / f"{iid}.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    trace = load_trace_events(inst_dir / "trace-agent.jsonl")
    terminal_events = {"max_steps_reached", "deadline", "provider_error"}
    terminal = [e for e in trace if e.get("event") in terminal_events]
    status = (report.get("agent") or {}).get("status", "")
    steps = (report.get("agent") or {}).get("steps", record.get("steps"))
    patch_bytes = report.get("patch_bytes", 0)

    ev = f"status={status} steps={steps} patch_bytes={patch_bytes}"
    if status and status != "completed":
        if status == "max_steps":
            reason = MAX_STEPS
        elif status == "loop_detected":
            reason = LOOP_DETECTED
        else:
            reason = RUN_ERROR
        if terminal:
            ev += f" terminal={terminal[-1]['event']}"
        out.update({"reason": reason, "evidence": ev})
        return out
    if patch_bytes == 0:
        out.update({"reason": EMPTY_PATCH, "evidence": ev})
        return out

    ftp = (report.get("evaluation") or {}).get("fail_to_pass", {})
    failed_ids = [k for k, ok in ftp.items() if not ok]
    detail = failing_tests_from_junit(inst_dir, failed_ids)
    if failed_ids:
        out.update(
            {
                "reason": TARGET_TESTS_FAILED,
                "evidence": ev,
                "failed_tests": detail,
            }
        )
        return out
    out.update(
        {
            "reason": REGRESSION_ONLY,
            "evidence": ev + f" regressions={report.get('ptp_regressions', [])}",
        }
    )
    return out


def plan_retry(diag: dict, *, base_max_steps: int = 30) -> dict | None:
    """按失败大类给出重试方案；不可重试返回 None。"""
    reason = diag.get("reason")
    if reason not in RETRYABLE:
        return None
    steps = base_max_steps
    hint = ""
    if reason == MAX_STEPS:
        steps = base_max_steps * 2
        hint = (
            "上一轮达到步数上限。请立刻聚焦最小可行修复：先给出改动，再给出最终报告，避免反复搜索。"
        )
    elif reason == TARGET_TESTS_FAILED:
        failed = diag.get("failed_tests") or []
        lines = [f"- {f['test']}: {f['message']}" for f in failed[:8]]
        hint = (
            "上一轮的改动已应用，但以下目标用例仍失败：\n"
            + "\n".join(lines)
            + "\n请基于现有改动继续修正，只改实现代码，完成后给出最终报告。"
        )
    elif reason == REGRESSION_ONLY:
        hint = (
            "上一轮改动让原本通过的用例出现回归（见证据）。"
            "请回退或收窄改动，保证不破坏既有行为，再给出最终报告。"
        )
    elif reason == EMPTY_PATCH:
        hint = "上一轮没有产生任何文件改动。请实际编辑源码修复问题，不要只做分析。"
    elif reason == LOOP_DETECTED:
        steps = base_max_steps
        hint = (
            "上一轮因重复相同动作被循环检测中止。请换一种做法："
            "直接读取目标文件并给出改动，不要反复做相同的搜索或列举。"
        )
    else:  # RUN_ERROR：多为模型/网络瞬时问题，原样重试
        steps = base_max_steps
        hint = "上一轮因模型侧瞬时错误中断，请基于现有状态继续完成修复。"
    return {"reason": reason, "max_steps": steps, "hint": hint}


def diagnose_all(*, root: Path, batch_path: Path | None = None) -> list[dict]:
    """对整张结果表做诊断，写 `diagnosis.json`。"""
    batch_path = batch_path or (Path(root).parent / "batch.json")
    records = json.loads(batch_path.read_text())
    diags = [classify(r, root=root) for r in records]
    diags.sort(key=lambda d: d["instance_id"])
    out = Path(root).parent / "diagnosis.json"
    out.write_text(json.dumps(diags, ensure_ascii=False, indent=2))
    return diags


def summarize(diags: list[dict]) -> dict:
    from collections import Counter

    counts = Counter(d["reason"] for d in diags)
    retryable = [d["instance_id"] for d in diags if d["reason"] in RETRYABLE]
    return {"total": len(diags), "by_reason": dict(counts), "retryable": retryable}
