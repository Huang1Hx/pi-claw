"""用同一任务、评分器和预算比较单 Agent、顺序工作流与主从多 Agent。"""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from math import ceil
from pathlib import Path
from statistics import median

from config import load_settings
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.context import ContextManager
from core.control import ExecutionControl
from core.prompts import build_system_prompt
from core.team import ReadOnlySubagentTeam, SubagentAssignment
from evaluation.mode_diagnostic import (
    FROZEN_STAGE3_FAILURES,
    build_diagnostic_prompt,
    extend_diagnostic_acquisition,
    load_diagnostic_grade_subset,
    merge_diagnostic_rows,
    merge_post_repair_diagnostic_rows,
    select_diagnostic_jobs,
    select_failed_diagnostic_jobs,
    summarize_diagnostic_acquisition,
    summarize_diagnostic_results,
)
from evaluation.mode_grading import GRADER_VERSION, calibrate, grade, safe_functions
from evaluation.mode_repair import (
    RepairJob,
    build_repair_evidence,
    build_repair_prompt,
    is_transient_provider_error,
    materialize_post_repair_rows,
    select_repair_jobs,
    summarize_repairs,
)
from evaluation.mode_reports import load_report_root, merge_subset, trial_identity
from evaluation.mode_tasks import TASKS
from evaluation.report import read_trace
from persistence.trace import TraceWriter, redact
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from security.workspace import Workspace
from tools import ToolExecutor

MODES = ("single", "workflow", "team")
VERIFY_ARGV = ["uv", "run", "python", "-m", "unittest", "-q", "test_target.py"]
PUBLIC_SUMMARY_FIELDS = (
    "schema_version",
    "grader_version",
    "generated_at",
    "real_model",
    "network_used",
    "source_sha256",
    "modes",
    "tasks",
    "trials_per_task",
    "workers",
    "deadline_seconds",
    "max_steps",
    "transport",
    "models",
    "premium_model",
    "accounting",
    "continuation",
    "acquisition",
    "repair",
    "diagnostic",
    "by_mode",
    "by_mode_and_task",
)
_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
WORKER_CLEANUP_RESERVE_SECONDS = 8.0


def worker_deadline_seconds(parent_deadline_seconds: float) -> float:
    """保留进程组、容器、快照和结果落盘窗口，不改变父级硬上限。"""
    return max(1.0, parent_deadline_seconds - WORKER_CLEANUP_RESERVE_SECONDS)


def team_assignments() -> tuple[SubagentAssignment, ...]:
    """主从模式的两份子 Agent 任务清单：scope 相同但交付物不同，因此不是重复任务。

    派发前去重按 ``work_key`` 判定；这里显式给出 ``objective`` 与 ``deliverable``，
    避免两份分析被误判为无意重复。
    """
    return (
        SubagentAssignment(
            "implementation",
            "实现分析",
            "只读检查 SPEC.md、solution.py 和 test_target.py。定位实现缺陷，"
            "给主 Agent 一份具体修改建议；不得写文件或运行命令。",
            objective="分析当前 solution.py 的实现缺陷",
            scope=("SPEC.md", "solution.py", "test_target.py"),
            deliverable="implementation_advice",
        ),
        SubagentAssignment(
            "risks",
            "边界与测试分析",
            "独立只读检查 SPEC.md、solution.py 和 test_target.py。推导公开测试之外的"
            "高风险边界与回归点；不得写文件或运行命令。",
            objective="分析当前 solution.py 的实现缺陷",
            scope=("SPEC.md", "solution.py", "test_target.py"),
            deliverable="risk_and_edge_cases",
        ),
    )


def save_json(path: Path, data) -> None:
    """所有评测产物统一脱敏；Settings、Key 与 URL 不进入报告。"""
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


def build_public_summary(report: dict) -> dict:
    """用字段白名单生成可提交摘要；逐 trial 路径、Trace 和连接信息一律不复制。"""
    public = {name: report[name] for name in PUBLIC_SUMMARY_FIELDS if name in report}
    repair = public.get("repair")
    if isinstance(repair, dict):
        # 未恢复项可公开身份与失败层，但本机 Trace 路径只留在完整报告。
        public["repair"] = json.loads(json.dumps(repair))
        for item in public["repair"].get("unrecovered", []):
            item.pop("traces", None)
    return public


def _report_acquisition(report: dict) -> dict:
    """优先沿用已有血缘成本；旧报告则从逐 trial 已知量建立保守下界。"""
    acquisition = report.get("acquisition")
    if isinstance(acquisition, dict) and type(acquisition.get("total_known_tokens")) is int:
        return {
            "total_known_tokens": acquisition["total_known_tokens"],
            "total_duration_seconds": float(acquisition.get("total_duration_seconds", 0)),
            "unknown_usage_trials": int(acquisition.get("unknown_usage_trials", 0)),
        }
    rows = report.get("trials")
    if not isinstance(rows, list):
        raise ValueError("报告缺少 trials，无法核算采集成本")
    return {
        "total_known_tokens": sum((row.get("usage") or {}).get("total_tokens", 0) for row in rows),
        "total_duration_seconds": round(
            sum(float(row.get("duration_seconds", 0)) for row in rows), 3
        ),
        "unknown_usage_trials": sum(bool(row.get("usage_may_be_incomplete")) for row in rows),
    }


def build_continuation_acquisition(source: dict, completed: dict) -> dict:
    """核算恢复血缘：原批次全部成本，加上原 provider error 身份的新补跑成本。"""
    source_rows = source.get("trials")
    completed_rows = completed.get("trials")
    if not isinstance(source_rows, list) or not isinstance(completed_rows, list):
        raise ValueError("恢复成本来源缺少 trials")
    source_map = {trial_identity(row): row for row in source_rows}
    completed_map = {trial_identity(row): row for row in completed_rows}
    if len(source_map) != len(source_rows) or len(completed_map) != len(completed_rows):
        raise ValueError("恢复成本来源包含重复 trial 身份")
    if set(source_map) != set(completed_map):
        raise ValueError("恢复前后 trial 身份集合不一致")
    retry_identities = {
        identity
        for identity, row in source_map.items()
        if row.get("leader_status") == "provider_error"
    }
    source_cost = _report_acquisition(source)
    retry_cost = _report_acquisition(
        {"trials": [completed_map[identity] for identity in sorted(retry_identities)]}
    )
    return {
        "source": source_cost,
        "retry": retry_cost,
        "total_known_tokens": source_cost["total_known_tokens"] + retry_cost["total_known_tokens"],
        "total_duration_seconds": round(
            source_cost["total_duration_seconds"] + retry_cost["total_duration_seconds"], 3
        ),
        "unknown_usage_trials": source_cost["unknown_usage_trials"]
        + retry_cost["unknown_usage_trials"],
        "note": "包含原批次全部 trial 与原 provider_error 身份的新补跑；保留项不重复计费",
    }


def _regrade_report_row(row: dict) -> dict:
    """从来源记录的只读工作区按当前评分器重建 trial 报告。"""
    identity = trial_identity(row)
    workspace_value = row.get("workspace")
    if not isinstance(workspace_value, str):
        raise ValueError(f"无法离线重评原 trial：{identity}")
    workspace = Path(workspace_value).resolve()
    folder = workspace.parent
    if not workspace.is_dir() or not (folder / "run-result.json").is_file():
        raise ValueError(f"无法离线重评原 trial：{identity}")
    return build_trial_report(
        folder,
        {
            "mode": row["mode"],
            "task": row["task"],
            "trial": row["trial"],
            "duration_seconds": float(row.get("duration_seconds", 0)),
        },
    )


def merge_report_roots(
    source_root: Path,
    replacement_root: Path,
    output: Path,
    *,
    replacement_origin_root: Path | None = None,
) -> dict:
    """离线合并两个真实批次，并在结束前复核来源文件没有发生变化。"""
    source, source_path, source_digest = load_report_root(source_root.resolve())
    replacement, replacement_path, replacement_digest = load_report_root(replacement_root.resolve())
    replacement_rows = replacement.get("trials")
    if replacement.get("grader_version") != GRADER_VERSION or (
        isinstance(replacement_rows, list)
        and any(
            not isinstance(row, dict) or row.get("grader_version") != GRADER_VERSION
            for row in replacement_rows
        )
    ):
        raise ValueError(f"替换报告评分器必须是当前 {GRADER_VERSION}")
    rows, merge_metadata = merge_subset(
        source,
        replacement,
        regrade=_regrade_report_row,
    )
    # 替换项虽然不重复评分，也必须指向仍可审计的工作区。
    replacement_identities = {trial_identity(row) for row in replacement.get("trials", [])}
    for row in rows:
        if trial_identity(row) in replacement_identities:
            _regrade_report_row(row)

    calibration = calibrate()
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止合并")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / "grader-calibration.json", calibration)

    source_cost = _report_acquisition(source)
    replacement_origin = None
    replacement_origin_path = None
    replacement_origin_digest = None
    if replacement_origin_root is not None:
        (
            replacement_origin,
            replacement_origin_path,
            replacement_origin_digest,
        ) = load_report_root(replacement_origin_root.resolve())
        replacement_cost = build_continuation_acquisition(
            replacement_origin,
            replacement,
        )
    else:
        replacement_cost = _report_acquisition(replacement)
    acquisition = {
        "source": source_cost,
        "replacement": replacement_cost,
        "total_known_tokens": source_cost["total_known_tokens"]
        + replacement_cost["total_known_tokens"],
        "total_duration_seconds": round(
            source_cost["total_duration_seconds"] + replacement_cost["total_duration_seconds"],
            3,
        ),
        "unknown_usage_trials": source_cost["unknown_usage_trials"]
        + replacement_cost["unknown_usage_trials"],
        "note": "包含完整来源批次与替换批次的采集成本；merged_trial_cost 另行汇总",
    }
    manifest = {
        "schema_version": 4,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": bool(source.get("real_model") or replacement.get("real_model")),
        "network_used": bool(source.get("network_used") or replacement.get("network_used")),
        "source_sha256": source.get("source_sha256"),
        "modes": source["modes"],
        "tasks": source["tasks"],
        "trials_per_task": source["trials_per_task"],
        "workers": source.get("workers"),
        "deadline_seconds": source["deadline_seconds"],
        "max_steps": source["max_steps"],
        "transport": source.get("transport"),
        "models": source["models"],
        "premium_model": source.get("premium_model"),
        "accounting": source.get(
            "accounting",
            "Token 统计包含主 Agent、全部子 Agent、工作流阶段与返工；不计算货币费用",
        ),
        "acquisition": acquisition,
        "merge": {
            **merge_metadata,
            "source_report": str(source_path),
            "source_report_sha256": source_digest,
            "source_grader_version": source.get("grader_version"),
            "replacement_report": str(replacement_path),
            "replacement_report_sha256": replacement_digest,
            "replacement_grader_version": replacement.get("grader_version"),
            **(
                {
                    "replacement_origin_report": str(replacement_origin_path),
                    "replacement_origin_report_sha256": replacement_origin_digest,
                }
                if replacement_origin_path is not None
                else {}
            ),
        },
    }
    premium_model = manifest["premium_model"]
    by_mode = {
        mode: summarize([row for row in rows if row["mode"] == mode], premium_model=premium_model)
        for mode in manifest["modes"]
    }
    by_mode_and_task = {
        mode: {
            task_id: summarize(
                [row for row in rows if row["mode"] == mode and row["task"] == task_id],
                premium_model=premium_model,
            )
            for task_id in manifest["tasks"]
        }
        for mode in manifest["modes"]
    }
    summary = {**manifest, "by_mode": by_mode, "by_mode_and_task": by_mode_and_task, "trials": rows}
    # 评分与汇总期间来源必须保持字节级不变，否则血缘指纹没有意义。
    if load_report_root(source_path.parent)[2] != source_digest:
        raise RuntimeError("合并期间来源 report.json 发生变化")
    if load_report_root(replacement_path.parent)[2] != replacement_digest:
        raise RuntimeError("合并期间替换 report.json 发生变化")
    if replacement_origin_path is not None and (
        load_report_root(replacement_origin_path.parent)[2] != replacement_origin_digest
    ):
        raise RuntimeError("合并期间替换来源 report.json 发生变化")
    save_json(output / "manifest.json", manifest)
    save_json(output / "report.json", summary)
    save_json(output / "public-summary.json", build_public_summary(summary))
    return summary


def _code_fingerprint() -> str:
    """对当前产品 Python 源码计算稳定指纹，区分不同实现生成的报告。"""
    source_root = Path(__file__).resolve().parents[1]
    return sha256(
        b"".join(
            path.relative_to(source_root).as_posix().encode() + path.read_bytes()
            for path in sorted(source_root.rglob("*.py"))
        )
    ).hexdigest()


def run_repair_report(source_root: Path, args) -> dict:
    """执行有界修复批次，写入独立账本并在结束时复核来源报告指纹。"""
    baseline, source_path, source_digest = load_report_root(source_root.resolve())
    if baseline.get("grader_version") != GRADER_VERSION:
        raise ValueError(
            f"修复来源评分器必须是 {GRADER_VERSION}，当前为 {baseline.get('grader_version')}"
        )
    # 在创建输出目录前验证身份、候选文件和模型配置，避免半成品目录。
    select_repair_jobs(baseline)
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        raise ValueError("修复需要已启用且模型完整的 planner/coder/reviewer 工作流配置")
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    if baseline.get("models") != models or baseline.get("premium_model") != premium_model:
        raise ValueError("修复必须沿用来源报告中的 DeepSeek/Kimi 模型分配")

    args.output = args.output.resolve()
    args.deadline = int(baseline["deadline_seconds"])
    args.max_steps = int(baseline["max_steps"])
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止修复")
    manifest = {
        "schema_version": 4,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": _code_fingerprint(),
        "modes": baseline["modes"],
        "tasks": baseline["tasks"],
        "trials_per_task": baseline["trials_per_task"],
        "workers": args.workers,
        "deadline_seconds": args.deadline,
        "max_steps": args.max_steps,
        "transport": {
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
        },
        "models": models,
        "premium_model": premium_model,
        "accounting": "首次采集与所有修复轮次分开核算；未知 Token 不按零处理",
        "repair_source": {
            "report": str(source_path),
            "report_sha256": source_digest,
            "max_rounds": args.repair_rounds,
        },
    }
    save_json(args.output / "manifest.json", manifest)
    ledger = execute_repair_rounds(baseline, args)
    metrics = summarize_repairs(baseline, ledger, premium_model=premium_model)
    if load_report_root(source_path.parent)[2] != source_digest:
        raise RuntimeError("修复期间来源 report.json 发生变化")
    ledger_document = {
        "schema_version": 1,
        "source_report": str(source_path),
        "source_report_sha256": source_digest,
        "entries": ledger,
    }
    summary = {**manifest, "repair": metrics, "ledger_entries": len(ledger)}
    save_json(args.output / "repair-ledger.json", ledger_document)
    save_json(args.output / "report.json", summary)
    save_json(args.output / "public-summary.json", build_public_summary(summary))
    return summary


def _load_json_with_digest(path: Path, label: str) -> tuple[dict, str]:
    """读取独立 JSON 账本并对原始字节做指纹，避免重序列化改变血缘。"""
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 {label}：{error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} 顶层必须是 JSON 对象")
    return value, sha256(raw).hexdigest()


def run_diagnostic_report(source_root: Path, args) -> dict:
    """独立复测冻结 12 个失败身份，不创建第三修复轮，也不改写 78/90 基线。"""
    source_root = source_root.resolve()
    source, source_path, source_digest = load_report_root(source_root)
    ledger_path = (source_root / "repair-ledger.json").resolve()
    ledger, ledger_digest = _load_json_with_digest(ledger_path, "repair-ledger.json")
    if source.get("grader_version") != GRADER_VERSION:
        raise ValueError(
            f"诊断来源评分器必须是 {GRADER_VERSION}，当前为 {source.get('grader_version')}"
        )
    if source.get("workers") != 1:
        raise ValueError("冻结诊断来源必须使用 workers=1")
    if source.get("deadline_seconds") != 420 or source.get("max_steps") != 24:
        raise ValueError("冻结诊断来源必须保持 deadline=420、max_steps=24")
    transport = source.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("诊断来源缺少传输重试协议")
    protocol = (
        transport.get("max_attempts_per_request"),
        transport.get("retry_base_delay_seconds"),
        transport.get("max_retry_delay_seconds"),
    )
    if protocol != (3, 2.0, 10.0):
        raise ValueError("冻结诊断来源必须保持 3 次尝试与 2/10 秒退避协议")

    repair_source = source.get("repair_source")
    if not isinstance(repair_source, dict):
        raise ValueError("诊断来源缺少 repair_source 血缘")
    baseline_value = repair_source.get("report")
    if not isinstance(baseline_value, str):
        raise ValueError("诊断来源缺少 baseline report 路径")
    baseline_path = Path(baseline_value).resolve()
    _baseline, resolved_baseline_path, baseline_digest = load_report_root(baseline_path.parent)
    if resolved_baseline_path != baseline_path:
        raise ValueError("诊断来源 baseline report 路径不一致")
    if baseline_digest != repair_source.get("report_sha256"):
        raise ValueError("诊断来源 baseline report 指纹不一致")
    ledger_source = ledger.get("source_report")
    if not isinstance(ledger_source, str) or Path(ledger_source).resolve() != baseline_path:
        raise ValueError("诊断账本与报告指向不同 baseline")
    if ledger.get("source_report_sha256") != baseline_digest:
        raise ValueError("诊断账本的 baseline 指纹不一致")
    jobs = select_diagnostic_jobs(source, ledger)

    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        raise ValueError("诊断需要已启用且模型完整的 planner/coder/reviewer 工作流配置")
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    if source.get("models") != models or source.get("premium_model") != premium_model:
        raise ValueError("诊断必须沿用来源报告中的 DeepSeek/Kimi 模型分配")

    # 诊断协议完全继承冻结来源；命令行同名默认值不能悄悄改变可比性。
    args.workers = 1
    args.deadline = 420
    args.max_steps = 24
    args.max_attempts = 3
    args.retry_base_delay = 2.0
    args.max_retry_delay = 10.0
    calibration = calibrate()
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止独立诊断")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    save_json(args.output / "grader-calibration.json", calibration)
    lineage = {
        "kind": "independent_stage3_diagnostic",
        "merge_into_baseline": False,
        "third_repair_round": False,
        "source_report": str(source_path),
        "source_report_sha256": source_digest,
        "source_ledger": str(ledger_path),
        "source_ledger_sha256": ledger_digest,
        "baseline_report": str(baseline_path),
        "baseline_report_sha256": baseline_digest,
    }
    manifest = {
        "schema_version": 5,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": _code_fingerprint(),
        "modes": list(dict.fromkeys(job.mode for job in jobs)),
        "tasks": list(dict.fromkeys(job.task for job in jobs)),
        "workers": 1,
        "deadline_seconds": 420,
        "max_steps": 24,
        "transport": dict(transport),
        "models": models,
        "premium_model": premium_model,
        "accounting": (
            "12 个独立诊断 trial 的主 Agent、子 Agent、工作流与返工全部计入；不回写 78/90"
        ),
        "diagnostic_lineage": lineage,
    }
    save_json(args.output / "manifest.json", manifest)
    rows = execute_diagnostic_jobs(jobs, args)
    diagnostic = summarize_diagnostic_results(rows)

    if load_report_root(source_path.parent)[2] != source_digest:
        raise RuntimeError("诊断期间来源 report.json 发生变化")
    if _load_json_with_digest(ledger_path, "repair-ledger.json")[1] != ledger_digest:
        raise RuntimeError("诊断期间来源 repair-ledger.json 发生变化")
    if load_report_root(baseline_path.parent)[2] != baseline_digest:
        raise RuntimeError("诊断期间 baseline report.json 发生变化")
    report = {**manifest, "diagnostic": diagnostic, "trials": rows}
    save_json(args.output / "report.json", report)
    save_json(args.output / "public-summary.json", build_public_summary(report))
    return report


def _load_post_repair_lineage(source: dict) -> dict[str, object]:
    """沿诊断血缘读取修复报告、账本和 90 项基线，并复核三重指纹。"""
    lineage = source.get("diagnostic_lineage")
    if not isinstance(lineage, dict):
        raise ValueError("诊断报告缺少 diagnostic_lineage")
    chained = lineage.get("kind") == "stage3_diagnostic_continuation_derived_90"
    repair_value = lineage.get("repair_report" if chained else "source_report")
    ledger_value = lineage.get("repair_ledger" if chained else "source_ledger")
    if not isinstance(repair_value, str) or not isinstance(ledger_value, str):
        raise ValueError("诊断血缘缺少修复报告或账本路径")
    repair_path = Path(repair_value).resolve()
    repair, resolved_repair_path, repair_digest = load_report_root(repair_path.parent)
    repair_digest_key = "repair_report_sha256" if chained else "source_report_sha256"
    if resolved_repair_path != repair_path or repair_digest != lineage.get(repair_digest_key):
        raise ValueError("诊断血缘中的修复报告路径或指纹不一致")
    ledger_path = Path(ledger_value).resolve()
    ledger, ledger_digest = _load_json_with_digest(ledger_path, "repair-ledger.json")
    ledger_digest_key = "repair_ledger_sha256" if chained else "source_ledger_sha256"
    if ledger_digest != lineage.get(ledger_digest_key):
        raise ValueError("诊断血缘中的 repair ledger 指纹不一致")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ValueError("repair-ledger.json 缺少 entries")

    repair_source = repair.get("repair_source")
    if not isinstance(repair_source, dict) or not isinstance(repair_source.get("report"), str):
        raise ValueError("修复报告缺少 baseline 血缘")
    baseline_path = Path(repair_source["report"]).resolve()
    baseline, resolved_baseline_path, baseline_digest = load_report_root(baseline_path.parent)
    if resolved_baseline_path != baseline_path or baseline_digest != repair_source.get(
        "report_sha256"
    ):
        raise ValueError("修复报告中的 baseline 路径或指纹不一致")
    ledger_source = ledger.get("source_report")
    if not isinstance(ledger_source, str) or Path(ledger_source).resolve() != baseline_path:
        raise ValueError("修复账本与报告指向不同 baseline")
    if ledger.get("source_report_sha256") != baseline_digest:
        raise ValueError("修复账本的 baseline 指纹不一致")
    if chained:
        declared_baseline = lineage.get("baseline_report")
        if (
            not isinstance(declared_baseline, str)
            or Path(declared_baseline).resolve() != baseline_path
            or lineage.get("baseline_report_sha256") != baseline_digest
        ):
            raise ValueError("链式诊断血缘中的 baseline 路径或指纹不一致")
    return {
        "repair": repair,
        "repair_path": repair_path,
        "repair_digest": repair_digest,
        "ledger": ledger,
        "ledger_path": ledger_path,
        "ledger_digest": ledger_digest,
        "entries": entries,
        "baseline": baseline,
        "baseline_path": baseline_path,
        "baseline_digest": baseline_digest,
    }


def _settings_models(args) -> tuple[dict[str, str], str | None]:
    """读取工作流模型 ID；连接地址和 Key 不进入返回值或评测报告。"""
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        raise ValueError("诊断续跑需要已启用且模型完整的 planner/coder/reviewer 配置")
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    return models, premium_model


def run_diagnostic_continuation(
    source_root: Path,
    replacement_root: Path | None,
    args,
) -> dict:
    """接续部分诊断结果，只跑当前失败项，并物化独立的派生 90-trial 报告。"""
    source, source_path, source_digest = load_report_root(source_root.resolve())
    if source.get("grader_version") != GRADER_VERSION:
        raise ValueError("诊断续跑必须沿用 grader 2.2")
    all_source_rows = source.get("trials")
    if not isinstance(all_source_rows, list):
        raise ValueError("诊断续跑来源缺少 trials")
    chained = (
        isinstance(source.get("diagnostic_lineage"), dict)
        and source["diagnostic_lineage"].get("kind") == "stage3_diagnostic_continuation_derived_90"
    )
    if chained:
        if replacement_root is not None or args.expected_diagnostic_replacements is not None:
            raise ValueError("链式诊断续跑直接使用派生报告，不能再提供部分替换目录")
        frozen = {item[:3] for item in FROZEN_STAGE3_FAILURES}
        source_rows = [row for row in all_source_rows if trial_identity(row) in frozen]
        merge_diagnostic_rows(source_rows, [])
        prior_rows: list[dict] = []
        prior_digest = None
    else:
        if replacement_root is None or args.expected_diagnostic_replacements is None:
            raise ValueError("首次诊断续跑需要部分替换目录和期望数量")
        source_rows = all_source_rows
        prior_rows, prior_digest = load_diagnostic_grade_subset(replacement_root)
        if len(prior_rows) != args.expected_diagnostic_replacements:
            raise ValueError(
                "部分重跑数量漂移："
                f"期望 {args.expected_diagnostic_replacements}，实际 {len(prior_rows)}"
            )
    current_rows = merge_diagnostic_rows(source_rows, prior_rows)
    jobs = select_failed_diagnostic_jobs(
        current_rows,
        expected_count=args.expected_diagnostic_failures,
    )

    if source.get("workers") != 1:
        raise ValueError("诊断续跑来源必须使用 workers=1")
    deadline = source.get("deadline_seconds")
    max_steps = source.get("max_steps")
    if type(deadline) is not int or not 30 <= deadline <= 1800:
        raise ValueError("诊断续跑 deadline 必须为 30–1800 秒")
    if type(max_steps) is not int or not 4 <= max_steps <= 40:
        raise ValueError("诊断续跑 max_steps 必须为 4–40")
    transport = source.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("诊断续跑来源缺少传输协议")
    protocol = (
        transport.get("max_attempts_per_request"),
        transport.get("retry_base_delay_seconds"),
        transport.get("max_retry_delay_seconds"),
    )
    if protocol != (3, 2.0, 10.0):
        raise ValueError("诊断续跑必须保持 3 次尝试与 2/10 秒退避协议")
    models, premium_model = _settings_models(args)
    if source.get("models") != models or source.get("premium_model") != premium_model:
        raise ValueError("诊断续跑必须沿用来源报告中的 DeepSeek/Kimi 模型分配")
    lineage = _load_post_repair_lineage(source)
    repair = lineage["repair"]
    baseline = lineage["baseline"]
    entries = lineage["entries"]
    if (
        not isinstance(repair, dict)
        or not isinstance(baseline, dict)
        or not isinstance(entries, list)
    ):
        raise ValueError("诊断血缘解析结果类型无效")
    post_repair_rows = materialize_post_repair_rows(baseline, entries)
    post_repair_successes = sum(row.get("passed") is True for row in post_repair_rows)
    if post_repair_successes != 78:
        raise ValueError(f"诊断续跑要求冻结 78/90，实际物化为 {post_repair_successes}/90")
    repair_metrics = repair.get("repair")
    if not isinstance(repair_metrics, dict) or not isinstance(
        repair_metrics.get("total_acquisition_cost"), dict
    ):
        raise ValueError("修复报告缺少历史总成本")
    historical_cost = repair_metrics["total_acquisition_cost"]
    previous_acquisition = None
    if chained:
        source_acquisition = source.get("acquisition")
        if not isinstance(source_acquisition, dict) or not isinstance(
            source_acquisition.get("diagnostic"), dict
        ):
            raise ValueError("链式诊断来源缺少完整 acquisition")
        if source_acquisition.get("historical_stage2") != historical_cost:
            raise ValueError("链式诊断来源的阶段 2 成本与冻结报告不一致")
        previous_acquisition = source_acquisition["diagnostic"]
        # 在校准和真实模型调用前先验证累计账本内部一致性。
        extend_diagnostic_acquisition(previous_acquisition, "preflight", [])
    prior_diagnostic_runs = (
        int(previous_acquisition.get("total_trial_runs", 0))
        if previous_acquisition is not None
        else 12 + len(prior_rows)
    )

    args.workers = 1
    args.deadline = deadline
    args.max_steps = max_steps
    args.max_attempts = 3
    args.retry_base_delay = 2.0
    args.max_retry_delay = 10.0
    calibration = calibrate()
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止诊断续跑")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    save_json(args.output / "grader-calibration.json", calibration)
    private_lineage = {
        "kind": "stage3_diagnostic_continuation_derived_90",
        "merge_into_frozen_baseline": False,
        "repair_report": str(lineage["repair_path"]),
        "repair_report_sha256": lineage["repair_digest"],
        "repair_ledger": str(lineage["ledger_path"]),
        "repair_ledger_sha256": lineage["ledger_digest"],
        "baseline_report": str(lineage["baseline_path"]),
        "baseline_report_sha256": lineage["baseline_digest"],
    }
    if chained:
        private_lineage.update(
            {
                "source_continuation": str(source_path),
                "source_continuation_sha256": source_digest,
            }
        )
    else:
        private_lineage.update(
            {
                "source_diagnostic": str(source_path),
                "source_diagnostic_sha256": source_digest,
                "prior_replacements": str(replacement_root.resolve()),
                "prior_replacements_sha256": prior_digest,
            }
        )
    manifest = {
        "schema_version": 6,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": _code_fingerprint(),
        "modes": baseline["modes"],
        "tasks": baseline["tasks"],
        "trials_per_task": baseline["trials_per_task"],
        "workers": 1,
        "deadline_seconds": deadline,
        "max_steps": max_steps,
        "transport": dict(transport),
        "models": models,
        "premium_model": premium_model,
        "accounting": (
            f"保留 78/90 历史总成本，并累计此前 {prior_diagnostic_runs} 次诊断运行"
            f"和本次 {args.expected_diagnostic_failures} 项重跑"
        ),
        "diagnostic_lineage": private_lineage,
    }
    save_json(args.output / "manifest.json", manifest)
    retry_rows = execute_diagnostic_jobs(
        jobs,
        args,
        expected_count=args.expected_diagnostic_failures,
    )
    latest_diagnostic_rows = merge_diagnostic_rows(current_rows, retry_rows)
    diagnostic = summarize_diagnostic_results(latest_diagnostic_rows)
    merged_rows = merge_post_repair_diagnostic_rows(
        post_repair_rows,
        latest_diagnostic_rows,
    )
    if previous_acquisition is None:
        diagnostic_acquisition = summarize_diagnostic_acquisition(
            [
                ("source_diagnostic", source_rows),
                ("prior_replacements", prior_rows),
                ("retry", retry_rows),
            ]
        )
    else:
        existing_labels = {
            stage.get("name")
            for stage in previous_acquisition.get("stages", [])
            if isinstance(stage, dict)
        }
        retry_number = 2
        while f"retry_{retry_number}" in existing_labels:
            retry_number += 1
        diagnostic_acquisition = extend_diagnostic_acquisition(
            previous_acquisition,
            f"retry_{retry_number}",
            retry_rows,
        )
    acquisition = {
        "historical_stage2": dict(historical_cost),
        "diagnostic": diagnostic_acquisition,
        "total_known_tokens": int(historical_cost.get("known_tokens", 0))
        + int(diagnostic_acquisition["total_known_tokens"]),
        "total_duration_seconds": round(
            float(historical_cost.get("duration_seconds", 0))
            + float(diagnostic_acquisition["total_duration_seconds"]),
            3,
        ),
        "unknown_usage_trials": int(historical_cost.get("unknown_usage_trials", 0))
        + int(diagnostic_acquisition["unknown_usage_trials"]),
        "note": "历史 78/90 与全部诊断尝试均计入；派生 90 行只表示每个身份的最新结果",
    }
    derived_successes = sum(row.get("passed") is True for row in merged_rows)
    continuation = {
        "source_post_repair_successes": post_repair_successes,
        "source_diagnostic_successes": sum(row.get("passed") is True for row in source_rows),
        "prior_replacement_runs": prior_diagnostic_runs - 12,
        "selected_retry_runs": len(retry_rows),
        "derived_successes": derived_successes,
        "derived_failures": len(merged_rows) - derived_successes,
        "frozen_78_of_90_unchanged": True,
    }
    by_mode = {
        mode: summarize(
            [row for row in merged_rows if row["mode"] == mode],
            premium_model=premium_model,
        )
        for mode in baseline["modes"]
    }
    by_mode_and_task = {
        mode: {
            task_id: summarize(
                [row for row in merged_rows if row["mode"] == mode and row["task"] == task_id],
                premium_model=premium_model,
            )
            for task_id in baseline["tasks"]
        }
        for mode in baseline["modes"]
    }

    if load_report_root(source_path.parent)[2] != source_digest:
        raise RuntimeError("诊断续跑期间来源 report.json 发生变化")
    if (
        replacement_root is not None
        and load_diagnostic_grade_subset(replacement_root)[1] != prior_digest
    ):
        raise RuntimeError("诊断续跑期间部分重跑 grade.json 发生变化")
    if load_report_root(Path(lineage["repair_path"]).parent)[2] != lineage["repair_digest"]:
        raise RuntimeError("诊断续跑期间修复 report.json 发生变化")
    if (
        _load_json_with_digest(Path(lineage["ledger_path"]), "repair-ledger.json")[1]
        != lineage["ledger_digest"]
    ):
        raise RuntimeError("诊断续跑期间 repair-ledger.json 发生变化")
    if load_report_root(Path(lineage["baseline_path"]).parent)[2] != lineage["baseline_digest"]:
        raise RuntimeError("诊断续跑期间 baseline report.json 发生变化")

    report = {
        **manifest,
        "continuation": continuation,
        "acquisition": acquisition,
        "repair": repair_metrics,
        "diagnostic": diagnostic,
        "by_mode": by_mode,
        "by_mode_and_task": by_mode_and_task,
        "trials": merged_rows,
    }
    save_json(args.output / "retry-results.json", retry_rows)
    save_json(args.output / "report.json", report)
    save_json(args.output / "public-summary.json", build_public_summary(report))
    return report


def run_repair_continuation(source_root: Path, args) -> dict:
    """在新目录续跑旧修复账本中的瞬时传输失败，并保留被替代尝试。"""
    source, source_path, source_digest = load_report_root(source_root.resolve())
    ledger_path = (source_root.resolve() / "repair-ledger.json").resolve()
    ledger_document, ledger_digest = _load_json_with_digest(ledger_path, "repair-ledger.json")
    entries = ledger_document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("repair-ledger.json 缺少 entries")
    if source.get("grader_version") != GRADER_VERSION:
        raise ValueError(
            f"修复续跑来源评分器必须是 {GRADER_VERSION}，当前为 {source.get('grader_version')}"
        )
    repair_source = source.get("repair_source")
    if not isinstance(repair_source, dict):
        raise ValueError("修复报告缺少 repair_source 血缘")
    baseline_path = Path(repair_source["report"]).resolve()
    baseline, resolved_baseline_path, baseline_digest = load_report_root(baseline_path.parent)
    if resolved_baseline_path != baseline_path:
        raise ValueError("baseline report 路径不一致")
    if baseline_digest != repair_source.get("report_sha256"):
        raise ValueError("baseline report 指纹与修复来源不一致")
    ledger_source_value = ledger_document.get("source_report")
    if not isinstance(ledger_source_value, str):
        raise ValueError("修复账本缺少 baseline report 路径")
    if Path(ledger_source_value).resolve() != baseline_path:
        raise ValueError("修复账本与修复报告指向不同 baseline")
    if ledger_document.get("source_report_sha256") != baseline_digest:
        raise ValueError("修复账本的基线指纹与修复报告不一致")

    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        raise ValueError("修复续跑需要已启用且模型完整的工作流配置")
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    if source.get("models") != models or source.get("premium_model") != premium_model:
        raise ValueError("修复续跑必须沿用来源报告中的 DeepSeek/Kimi 模型分配")

    args.output = args.output.resolve()
    args.deadline = int(source["deadline_seconds"])
    args.max_steps = int(source["max_steps"])
    args.repair_rounds = int(repair_source.get("max_rounds", 2))
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止修复续跑")
    continuation = {
        "source_report": str(source_path),
        "source_report_sha256": source_digest,
        "source_ledger": str(ledger_path),
        "source_ledger_sha256": ledger_digest,
        "selected_failure": "transient_provider_error",
    }
    manifest = {
        "schema_version": 4,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": _code_fingerprint(),
        "modes": baseline["modes"],
        "tasks": baseline["tasks"],
        "trials_per_task": baseline["trials_per_task"],
        "workers": args.workers,
        "deadline_seconds": args.deadline,
        "max_steps": args.max_steps,
        "transport": {
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
        },
        "models": models,
        "premium_model": premium_model,
        "accounting": "旧修复轮次、被替代传输失败与新续跑全部计入；未知 Token 不按零处理",
        "repair_source": repair_source,
        "repair_continuation": continuation,
    }
    save_json(args.output / "manifest.json", manifest)
    updated_entries = resume_repair_transport(baseline, entries, args)
    metrics = summarize_repairs(baseline, updated_entries, premium_model=premium_model)

    if load_report_root(source_path.parent)[2] != source_digest:
        raise RuntimeError("续跑期间来源修复 report.json 发生变化")
    if _load_json_with_digest(ledger_path, "repair-ledger.json")[1] != ledger_digest:
        raise RuntimeError("续跑期间来源 repair-ledger.json 发生变化")
    if load_report_root(baseline_path.parent)[2] != baseline_digest:
        raise RuntimeError("续跑期间 baseline report.json 发生变化")
    output_ledger = {
        "schema_version": 1,
        "source_report": str(baseline_path),
        "source_report_sha256": baseline_digest,
        "continuation": continuation,
        "entries": updated_entries,
    }
    summary = {**manifest, "repair": metrics, "ledger_entries": len(updated_entries)}
    save_json(args.output / "repair-ledger.json", output_ledger)
    save_json(args.output / "report.json", summary)
    save_json(args.output / "public-summary.json", build_public_summary(summary))
    return summary


def select_resume_jobs(report: dict) -> list[tuple[str, str, int]]:
    """只选择模型传输失败项；语义失败、循环和未验证结果不得通过续跑洗掉。"""
    rows = report.get("trials")
    if not isinstance(rows, list):
        raise ValueError("续跑来源缺少 trials")
    identities = [trial_identity(row) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("续跑来源包含重复 trial 身份")
    return [
        identity
        for row, identity in zip(rows, identities, strict=True)
        if row.get("leader_status") == "provider_error"
    ]


def combine_resumed_reports(source_report: dict, replacements: list[dict]) -> list[dict]:
    """替换传输失败项，并用当前评分器离线重评其余 trial；原目录和 Trace 保持不变。"""
    source_rows = source_report.get("trials")
    if not isinstance(source_rows, list):
        raise ValueError("续跑来源缺少 trials")
    expected = set(select_resume_jobs(source_report))
    replacement_map = {trial_identity(row): row for row in replacements}
    if len(replacement_map) != len(replacements) or set(replacement_map) != expected:
        raise ValueError("恢复结果与 provider_error trial 清单不一致")

    combined = []
    for row in source_rows:
        identity = trial_identity(row)
        if identity in replacement_map:
            combined.append(replacement_map[identity])
            continue
        workspace = Path(row["workspace"])
        folder = workspace.parent
        if not workspace.is_dir() or not (folder / "run-result.json").is_file():
            raise ValueError(f"无法离线重评原 trial：{identity}")
        combined.append(
            build_trial_report(
                folder,
                {
                    "mode": row["mode"],
                    "task": row["task"],
                    "trial": row["trial"],
                    "duration_seconds": row["duration_seconds"],
                },
            )
        )
    return combined


def approval_for(task_id: str, workspace: Path):
    """唯一写者只可修改 solution.py，并仅可执行固定公开测试。"""
    task = TASKS[task_id]

    def approve(call, decision, preview=None):
        """Policy 的 DENY 不会进入回调；这里进一步收窄允许副作用。"""
        args = call.arguments
        if call.name in {"write_or_replace", "apply_patch"}:
            if args.get("path") not in {"solution.py", "./solution.py"}:
                return "评测中只能修改 solution.py；SPEC.md、test_target.py 和其他文件均为只读"
            return True
        if call.name != "run_command":
            return False
        if args.get("argv") != VERIFY_ARGV:
            return f"验证命令必须精确使用 argv={VERIFY_ARGV!r}，不得增删参数或改用其他命令"
        try:
            boundary = Workspace(workspace)
            if boundary.resolve(args.get("cwd", ".")) != boundary.root:
                return "验证命令的 cwd 必须是 trial 工作区根目录（使用 '.' 或省略 cwd）"
            if (workspace / "SPEC.md").read_text() != task.spec:
                return False
            if (workspace / "test_target.py").read_text() != task.public_test:
                return False
            safe_functions((workspace / "solution.py").read_text(), task.function_name)
        except (SyntaxError, TypeError, ValueError) as error:
            return f"候选代码未通过受限语法检查：{error}"
        except OSError as error:
            return f"评测文件读取失败：{error}"
        return True

    return approve


def _provider(settings, profile: str, args) -> RoutedProvider:
    """每个 Agent 使用独立 Provider 状态；相同连接仅共享配置值。"""
    return RoutedProvider(
        settings,
        mode=profile,
        max_attempts=args.max_attempts,
        retry_base_delay=args.retry_base_delay,
        max_retry_delay=args.max_retry_delay,
    )


def _executor(workspace: Path, task_id: str, *, writable: bool) -> ToolExecutor:
    """主 Agent 可进入精确审批；子 Agent 从策略层保持只读且禁止命令。"""
    return ToolExecutor(
        WorkspacePolicy(
            workspace,
            allow_write=writable,
            allow_commands=writable,
            command_profile="coding" if writable else "minimal",
        ),
        approval_callback=approval_for(task_id, workspace) if writable else None,
        command_timeout=10,
        max_write_bytes=12_000,
    )


def _agent(provider, executor, trace: Path, workspace: Path, *, steps: int) -> Agent:
    """所有模式复用相同 Agent Loop、上下文与完成证据检查。"""
    return Agent(
        provider,
        executor,
        max_steps=steps,
        trace=TraceWriter(trace),
        system_prompt=build_system_prompt(workspace),
        context_manager=ContextManager(),
        completion_verifier=ToolEvidenceCompletionVerifier(),
        streaming=False,
    )


def worker(args) -> None:
    """在 trial 子进程内运行一种架构；每个 Agent 都有独立 Provider、上下文和 Trace。"""
    folder = args.output.resolve()
    workspace = folder / "workspace"
    task = TASKS[args.worker]
    prompt = task.prompt
    if args.worker_prompt_file:
        prompt_path = args.worker_prompt_file.resolve()
        if prompt_path.parent != folder:
            raise ValueError("worker prompt 必须位于当前 trial 目录")
        prompt = prompt_path.read_text()
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    # 对照关注架构，不额外调用模型做意图分类；工作流仍使用当前角色和返工上限。
    workflow_settings = replace(
        settings,
        routing=replace(settings.routing, intent_strategy="rules"),
    )
    profile_map = {
        "single": [settings.workflow.coder],
        "workflow": [
            settings.workflow.planner,
            settings.workflow.coder,
            settings.workflow.reviewer,
        ],
        "team": [
            settings.workflow.coder,
            settings.workflow.planner,
            settings.workflow.reviewer,
        ],
    }
    save_json(
        folder / "config-snapshot.json",
        {
            "mode": args.mode,
            "profiles": [
                {
                    "profile": name,
                    "model": settings.profiles[name].model,
                    "thinking_mode": settings.profiles[name].thinking_mode,
                    "thinking_format": settings.profiles[name].thinking_format,
                }
                for name in profile_map[args.mode]
            ],
            "max_steps": args.max_steps,
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
            "command_timeout_seconds": 10,
            "workflow": {
                "max_planning_requests": settings.workflow.max_planning_requests,
                "max_review_requests": settings.workflow.max_review_requests,
                "max_review_rounds": settings.workflow.max_review_rounds,
            }
            if args.mode == "workflow"
            else None,
        },
    )
    started = time.monotonic()
    # 父级 420 秒硬上限保持不变；提前停止新工作，为多层清理和结果落盘预留窗口。
    control = ExecutionControl(deadline_monotonic=started + worker_deadline_seconds(args.deadline))
    writable = _executor(workspace, args.worker, writable=True)
    if args.mode == "single":
        result = _agent(
            _provider(settings, settings.workflow.coder, args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        ).run(prompt, control=control)
        payload = {
            "mode": args.mode,
            "leader": result.to_dict(),
            "subagents": [],
            "usage": result.usage,
            "duration_seconds": round(time.monotonic() - started, 3),
            "max_parallel_subagents": 0,
        }
    elif args.mode == "workflow":
        result = _agent(
            _provider(workflow_settings, "auto", args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        ).run(prompt, control=control)
        payload = {
            "mode": args.mode,
            "leader": result.to_dict(),
            "subagents": [],
            "usage": result.usage,
            "duration_seconds": round(time.monotonic() - started, 3),
            "max_parallel_subagents": 0,
        }
    else:
        leader = _agent(
            _provider(settings, settings.workflow.coder, args),
            writable,
            folder / "trace-leader.jsonl",
            workspace,
            steps=args.max_steps,
        )
        implementation = _agent(
            _provider(settings, settings.workflow.planner, args),
            _executor(workspace, args.worker, writable=False),
            folder / "trace-subagent-implementation.jsonl",
            workspace,
            steps=6,
        )
        risks = _agent(
            _provider(settings, settings.workflow.reviewer, args),
            _executor(workspace, args.worker, writable=False),
            folder / "trace-subagent-risks.jsonl",
            workspace,
            steps=6,
        )
        team = ReadOnlySubagentTeam(
            leader,
            {"implementation": implementation, "risks": risks},
            max_workers=2,
        )
        result = team.run(prompt, team_assignments(), control=control)
        payload = {"mode": args.mode, **result.to_dict()}
    payload["duration_seconds"] = round(time.monotonic() - started, 3)
    save_json(folder / "run-result.json", payload)


def _events(folder: Path, mode: str, result: dict | None = None) -> tuple[list[dict], list[dict]]:
    """读取所有参与者 Trace，给每条事件补 participant；损坏项不阻断其他 Trace。"""
    events, issues = [], []
    paths = sorted(folder.glob("trace-*.jsonl"))
    expected = _expected_participants(mode, result)
    found = {path.stem.removeprefix("trace-") for path in paths}
    for participant in sorted(expected - found):
        issues.append({"participant": participant, "line": None, "error": "trace missing"})
    for path in paths:
        participant = path.stem.removeprefix("trace-")
        rows, errors = read_trace(path)
        if not rows:
            issues.append({"participant": participant, "line": None, "error": "trace empty"})
        events.extend(
            {**row, "participant": participant, "trace": str(path.resolve())} for row in rows
        )
        issues.extend(
            {**error, "participant": participant, "trace": str(path.resolve())} for error in errors
        )
    return events, issues


def _expected_participants(mode: str, result: dict | None = None) -> set[str]:
    """按实际派发集合推导参与者；被去重拦截的子 Agent 不产生 Trace 也不计入预期。

    单 Agent 与顺序工作流仍只有一个 leader。主从模式优先读取 run-result 的
    ``dispatch.dispatched_assignments``；缺失时退回 mode 的确定性默认集合，保持
    旧产物与既有回归兼容。
    """
    expected = {"leader"}
    if mode != "team":
        return expected
    dispatch = result.get("dispatch") if isinstance(result, dict) else None
    dispatched = dispatch.get("dispatched_assignments") if isinstance(dispatch, dict) else None
    if isinstance(dispatched, list) and all(isinstance(name, str) for name in dispatched):
        expected |= {f"subagent-{name}" for name in dispatched}
    else:
        expected |= {"subagent-implementation", "subagent-risks"}
    return expected


def _partial_usage(events: list[dict], mode: str, result: dict | None = None) -> dict:
    """worker 被期限/中断终止时，从每个 Trace 最后一次累计值恢复已知 Token。"""
    latest = {}
    for event in events:
        if event["event"] == "usage_updated" and isinstance(event["payload"], dict):
            latest[event["participant"]] = event["payload"]
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "requests",
        "requests_with_usage",
        "requests_without_usage",
        "retries",
    )
    total = {key: sum(int(row.get(key, 0)) for row in latest.values()) for key in keys}
    # 没有完整 run-result 就无法确认被终止瞬间的在途请求；未知量绝不按已知零处理。
    total["unknown_participants"] = len(_expected_participants(mode, result))
    return total


def _attempted_requests(result: dict, usage: dict) -> int:
    """汇总主从路由已发起的逻辑请求；超时请求没有 ModelReply，但仍属于总消耗缺口。"""
    participants = [result.get("leader", {})]
    participants.extend(
        item.get("result", {}) for item in result.get("subagents", []) if isinstance(item, dict)
    )
    attempted = 0
    for participant in participants:
        routing = participant.get("routing") if isinstance(participant, dict) else None
        participant_usage = participant.get("usage") if isinstance(participant, dict) else None
        if isinstance(routing, dict) and type(routing.get("requests")) is int:
            attempted += routing["requests"]
        elif isinstance(participant_usage, dict):
            attempted += int(participant_usage.get("requests", 0))
    return max(attempted, int(usage.get("requests", 0)))


def _model_usage(result: dict, events: list[dict]) -> dict[str, dict[str, int]]:
    """按具体模型核算已知 Token，并把已路由但无 usage 的请求保留为缺口。"""
    attempted = Counter()
    profile_models = {}
    for event in events:
        if event["event"] != "model_routed":
            continue
        profile = event["payload"].get("profile")
        model = event["payload"].get("model")
        if isinstance(profile, str) and isinstance(model, str):
            profile_models[(event["participant"], profile)] = model
            attempted[model] += 1

    participants = [("leader", result.get("leader", {}))]
    participants.extend(
        (f"subagent-{item.get('name')}", item.get("result", {}))
        for item in result.get("subagents", [])
        if isinstance(item, dict)
    )
    known: dict[str, Counter] = {}
    for participant_name, participant in participants:
        routing = participant.get("routing") if isinstance(participant, dict) else None
        models = routing.get("models") if isinstance(routing, dict) else None
        if not isinstance(models, dict):
            continue
        for profile, usage in models.items():
            if not isinstance(usage, dict):
                continue
            model = profile_models.get((participant_name, profile))
            if model is None:
                candidates = {
                    value
                    for (event_participant, event_profile), value in profile_models.items()
                    if event_profile == profile
                }
                model = candidates.pop() if len(candidates) == 1 else f"profile:{profile}"
            totals = known.setdefault(model, Counter())
            totals["requests_with_usage"] += int(usage.get("requests", 0))
            totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
            totals["completion_tokens"] += int(usage.get("completion_tokens", 0))

    rows = {}
    for model in sorted(set(attempted) | set(known)):
        values = known.get(model, Counter())
        with_usage = values["requests_with_usage"]
        attempted_requests = max(attempted[model], with_usage)
        rows[model] = {
            "attempted_requests": attempted_requests,
            "requests_with_usage": with_usage,
            "requests_without_usage": max(0, attempted_requests - with_usage),
            "prompt_tokens": values["prompt_tokens"],
            "completion_tokens": values["completion_tokens"],
            "total_tokens": values["prompt_tokens"] + values["completion_tokens"],
        }
    return rows


def _reconcile_usage(result: dict, usage: dict) -> dict:
    """把已知 Token 与无 usage 的已发起请求分开；未知请求绝不虚构 Token。"""
    reconciled = dict(usage)
    attempted = _attempted_requests(result, reconciled)
    with_usage = int(reconciled.get("requests_with_usage", 0))
    reconciled["attempted_requests"] = attempted
    reconciled["requests"] = attempted
    reconciled["requests_without_usage"] = max(
        int(reconciled.get("requests_without_usage", 0)), attempted - with_usage
    )
    return reconciled


def _public_test_passed(events: list[dict]) -> bool:
    """严格成功要求唯一写者真实执行过固定公开测试且返回成功。"""
    return any(
        event["participant"] == "leader"
        and event["event"] == "tool_result"
        and event["payload"].get("tool_name") == "run_command"
        and event["payload"].get("ok") is True
        and event["payload"].get("output", {}).get("argv") == VERIFY_ARGV
        for event in events
    )


def build_trial_report(folder: Path, metadata: dict) -> dict:
    """统一评分三种模式，并从所有参与者 Trace 统计越权、冲突和返工。"""
    result_path = folder / "run-result.json"
    try:
        result = json.loads(result_path.read_text())
        leader = result["leader"]
        if not isinstance(leader.get("status"), str):
            raise ValueError("invalid leader status")
    except (OSError, ValueError, KeyError, TypeError):
        result = {"leader": {"status": "harness_error"}, "usage": None, "subagents": []}
        leader = result["leader"]
    events, trace_issues = _events(folder, metadata["mode"], result)
    checks = grade(metadata["task"], folder / "workspace", leader["status"])
    checks.insert(
        2,
        {
            "name": "public_test_passed",
            "passed": _public_test_passed(events),
            "layer": "L0_execution",
            "detail": "必须由 leader 成功执行固定 unittest argv",
        },
    )
    checks.insert(
        1,
        {
            "name": "trace_integrity",
            "passed": not trace_issues,
            "layer": "L0_execution",
            "detail": json.dumps(trace_issues, ensure_ascii=False) if trace_issues else "",
        },
    )
    tool_results = [event for event in events if event["event"] == "tool_result"]
    unauthorized = {}
    for event in tool_results:
        payload = event["payload"]
        if payload.get("metadata", {}).get("policy", {}).get("action") == "deny" or (
            payload.get("error") or ""
        ).startswith("未获批准："):
            unauthorized[(event["participant"], payload.get("call_id"))] = event
    # 子 Agent 的副作用提议即使只在 model_reply 中出现，也属于越权尝试。
    for event in events:
        if not event["participant"].startswith("subagent-") or event["event"] != "model_reply":
            continue
        calls = event["payload"].get("tool_calls") or []
        if event["payload"].get("tool_call"):
            calls = [event["payload"]["tool_call"]]
        for call in calls:
            if call.get("name") in {"write_or_replace", "apply_patch", "run_command"}:
                unauthorized.setdefault((event["participant"], call.get("id")), event)
    # 顺序工作流会在 Provider 边界拒绝只读阶段的副作用，此时不会生成 ToolResult。
    for event in events:
        if event["event"] == "run_unverified" and "非只读工具" in (
            event["payload"].get("error") or ""
        ):
            unauthorized.setdefault((event["participant"], event["step"]), event)
    conflicts = [
        event
        for event in tool_results
        if "目标文件发生变化" in (event["payload"].get("error") or "")
    ]
    rework = sum(event["event"] == "completion_rejected" for event in events)
    rework += sum(
        event["event"] == "workflow_artifact"
        and event["payload"].get("kind") == "review"
        and event["payload"].get("data", {}).get("verdict") == "changes_requested"
        for event in events
    )
    passed = all(item["passed"] is True for item in checks)
    usage = result.get("usage")
    usage_source = "run_result"
    if usage is None:
        usage = _partial_usage(events, metadata["mode"], result)
        usage_source = "partial_trace"
    else:
        usage = _reconcile_usage(result, usage)
    model_usage = _model_usage(result, events)
    models = sorted(model_usage)
    model_requests = {model: values["attempted_requests"] for model, values in model_usage.items()}
    return {
        **metadata,
        "grader_version": GRADER_VERSION,
        "passed": passed,
        "leader_status": leader["status"],
        "checks": checks,
        "passed_checks": sum(item["passed"] is True for item in checks),
        "total_checks": len(checks),
        "usage": usage,
        "usage_source": usage_source,
        "usage_may_be_incomplete": usage_source == "partial_trace"
        or bool(usage.get("unknown_participants"))
        or bool(usage.get("requests_without_usage")),
        "duration_seconds": result.get("duration_seconds", metadata["duration_seconds"]),
        "models_used": models,
        "model_requests": model_requests,
        "model_usage": model_usage,
        "participants": len(_expected_participants(metadata["mode"], result)),
        "dispatch": result.get("dispatch") or {},
        "duplicate_assignments_detected": int(
            (result.get("dispatch") or {}).get("duplicate_assignments_detected", 0)
        ),
        "duplicate_assignments_blocked": int(
            (result.get("dispatch") or {}).get("duplicate_assignments_blocked", 0)
        ),
        "intentional_redundancy_runs": int(
            (result.get("dispatch") or {}).get("intentional_redundancy_runs", 0)
        ),
        "duplicate_findings_collapsed": int(
            (result.get("dispatch") or {}).get("duplicate_findings_collapsed", 0)
        ),
        "handoff_artifact_count": int(
            (result.get("dispatch") or {}).get(
                "handoff_artifact_count", len(result.get("subagents", []))
            )
        ),
        "max_parallel_subagents": result.get("max_parallel_subagents", 0),
        "tool_calls": len(tool_results),
        "unauthorized_attempts": len(unauthorized),
        "write_conflicts": len(conflicts),
        "rework_events": rework,
        "trace_issues": trace_issues,
        "workspace": str((folder / "workspace").resolve()),
        "traces": [str(path.resolve()) for path in sorted(folder.glob("trace-*.jsonl"))],
        "failure_checks": [item for item in checks if item["passed"] is not True],
    }


def reliability(rows: list[dict]) -> dict:
    """按任务分组计算实测 pass@k/pass^k；不同模式绝不混成重复试验。"""
    grouped = {}
    for row in rows:
        grouped.setdefault(row["task"], []).append(row["passed"])
    counts = {len(values) for values in grouped.values()}
    if not grouped or len(counts) != 1:
        raise ValueError("每个任务必须有相同且非零的重复次数")
    k = counts.pop()
    return {
        "tasks": len(grouped),
        "trials": len(rows),
        "k": k,
        "strict_success_rate": sum(row["passed"] for row in rows) / len(rows),
        f"observed_pass_at_{k}": sum(any(values) for values in grouped.values()) / len(grouped),
        f"observed_pass_power_{k}": sum(all(values) for values in grouped.values()) / len(grouped),
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    """计算不插值的最近秩分位数；P95 会保留真实观测到的长尾值。"""
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def summarize(rows: list[dict], *, premium_model: str | None = None) -> dict:
    """汇总成功、长尾耗时、模型总请求、Token、安全边界和返工。"""
    successes = sum(row["passed"] for row in rows)
    known_tokens = sum((row["usage"] or {}).get("total_tokens", 0) for row in rows)
    durations = [row["duration_seconds"] for row in rows]
    statuses = Counter(row.get("leader_status", "unknown") for row in rows)
    failure_layers = Counter(
        item.get("layer", "unknown")
        for row in rows
        for item in row.get("failure_checks", [])
        if item.get("passed") is not True
    )
    model_requests = Counter()
    model_usage: dict[str, Counter] = {}
    for row in rows:
        model_requests.update(row.get("model_requests", {}))
        for model, usage in row.get("model_usage", {}).items():
            totals = model_usage.setdefault(model, Counter())
            totals.update(usage)
    model_usage_distribution = {
        model: {
            name: values[name]
            for name in (
                "attempted_requests",
                "requests_with_usage",
                "requests_without_usage",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            )
        }
        for model, values in sorted(model_usage.items())
    }
    empty_model_usage = {
        "attempted_requests": 0,
        "requests_with_usage": 0,
        "requests_without_usage": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    timeout_trials = statuses["deadline"]
    return {
        "models_used": sorted({model for row in rows for model in row["models_used"]}),
        "model_request_distribution": dict(sorted(model_requests.items())),
        "premium_model_requests": model_requests[premium_model] if premium_model else 0,
        "model_usage_distribution": model_usage_distribution,
        "premium_model_usage": model_usage_distribution.get(premium_model, empty_model_usage)
        if premium_model
        else empty_model_usage,
        "reliability": reliability(rows),
        "strict_successes": successes,
        "strict_failures": len(rows) - successes,
        "p50_duration_seconds": round(median(durations), 3),
        "p95_duration_seconds": round(_nearest_rank(durations, 0.95), 3),
        "median_duration_seconds": round(median(durations), 3),
        "total_duration_seconds": round(sum(durations), 3),
        "timeout_trials": timeout_trials,
        "timeout_rate": round(timeout_trials / len(rows), 6),
        "status_distribution": dict(sorted(statuses.items())),
        "failure_layer_distribution": dict(sorted(failure_layers.items())),
        "known_total_tokens": known_tokens,
        "unattributed_known_tokens": max(
            0,
            known_tokens
            - sum(values["total_tokens"] for values in model_usage_distribution.values()),
        ),
        "unknown_usage_trials": sum(row["usage_may_be_incomplete"] for row in rows),
        "known_tokens_per_strict_success": round(known_tokens / successes, 2)
        if successes
        else None,
        "tokens_per_success_is_lower_bound": any(row["usage_may_be_incomplete"] for row in rows),
        "tool_calls": sum(row["tool_calls"] for row in rows),
        "unauthorized_attempts": sum(row["unauthorized_attempts"] for row in rows),
        "write_conflicts": sum(row["write_conflicts"] for row in rows),
        "rework_events": sum(row["rework_events"] for row in rows),
        "max_parallel_subagents": max(row["max_parallel_subagents"] for row in rows),
        "duplicate_assignments_detected": sum(
            row.get("duplicate_assignments_detected", 0) for row in rows
        ),
        "duplicate_assignments_blocked": sum(
            row.get("duplicate_assignments_blocked", 0) for row in rows
        ),
        "intentional_redundancy_runs": sum(
            row.get("intentional_redundancy_runs", 0) for row in rows
        ),
        "duplicate_findings_collapsed": sum(
            row.get("duplicate_findings_collapsed", 0) for row in rows
        ),
        "handoff_artifact_count": sum(row.get("handoff_artifact_count", 0) for row in rows),
    }


def run_trial(
    mode: str,
    task_id: str,
    number: int,
    args,
    *,
    folder: Path | None = None,
    seed_solution: str | None = None,
    prompt: str | None = None,
    trial_metadata: dict | None = None,
    trial_metadata_label: str = "repair",
    prompt_filename: str = "repair-prompt.txt",
) -> dict:
    """父进程提供 trial 墙钟期限；超时清理整个 worker 进程组并保留失败证据。"""
    if trial_metadata_label not in {"repair", "diagnostic"}:
        raise ValueError("trial 元数据标签只能是 repair 或 diagnostic")
    if prompt_filename not in {"repair-prompt.txt", "diagnostic-prompt.txt"}:
        raise ValueError("trial 提示文件名不在白名单")
    folder = folder or args.output / mode / f"{task_id}-{number}"
    folder.mkdir(parents=True)
    workspace = folder / "workspace"
    workspace.mkdir()
    for name, content in TASKS[task_id].files.items():
        (workspace / name).write_text(content)
    if seed_solution is not None:
        (workspace / "solution.py").write_text(seed_solution)
    task_prompt = prompt or TASKS[task_id].prompt
    save_json(
        folder / "task.json",
        {
            "task": task_id,
            "title": TASKS[task_id].title,
            "prompt": task_prompt,
            **({trial_metadata_label: trial_metadata} if trial_metadata is not None else {}),
        },
    )
    prompt_file = None
    if prompt is not None:
        prompt_file = folder / prompt_filename
        prompt_file.write_text(prompt)
    print(f"[开始] mode={mode} task={task_id} trial={number}", flush=True)
    command = [
        sys.executable,
        "-m",
        "evaluation.compare_modes",
        "--worker",
        task_id,
        "--mode",
        mode,
        "--output",
        str(folder.resolve()),
        "--config",
        str(args.config.resolve()),
        "--max-steps",
        str(args.max_steps),
        "--deadline",
        str(args.deadline),
        "--max-attempts",
        str(args.max_attempts),
        "--retry-base-delay",
        str(args.retry_base_delay),
        "--max-retry-delay",
        str(args.max_retry_delay),
    ]
    if prompt_file is not None:
        command.extend(["--worker-prompt-file", str(prompt_file.resolve())])
    started = time.monotonic()
    timed_out = False
    with (folder / "progress.log").open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=stream, start_new_session=True)
        with _PROCESS_LOCK:
            _ACTIVE_PROCESSES.add(process)
        try:
            try:
                process.wait(timeout=args.deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except BaseException:
                # 串行诊断会在主线程等待 worker；Ctrl-C 等异常必须在移出活动集前清理进程组。
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                raise
        finally:
            with _PROCESS_LOCK:
                _ACTIVE_PROCESSES.discard(process)
    progress = folder / "progress.log"
    progress.write_text(redact(progress.read_text(errors="replace")))
    result_path = folder / "run-result.json"
    if not result_path.exists():
        save_json(
            result_path,
            {
                "mode": mode,
                "leader": {
                    "status": "deadline" if timed_out else "harness_error",
                    "process_returncode": process.returncode,
                },
                "usage": None,
                "subagents": [],
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )
    report = build_trial_report(
        folder,
        {
            "mode": mode,
            "task": task_id,
            "trial": number,
            "duration_seconds": round(time.monotonic() - started, 3),
        },
    )
    save_json(folder / "grade.json", report)
    print(
        f"[评分] mode={mode} task={task_id} trial={number}: "
        f"passed={report['passed']} checks={report['passed_checks']}/{report['total_checks']} "
        f"tokens={(report['usage'] or {}).get('total_tokens', 'unknown')} "
        f"duration={report['duration_seconds']}s",
        flush=True,
    )
    return report


def run_repair_round(job: RepairJob, round_number: int, args) -> dict:
    """用父候选与脱敏 Trace 运行一个保持原模式的独立修复 trial。"""
    evidence = build_repair_evidence(job.source_row)
    prompt = build_repair_prompt(job, evidence, round_number)
    seed = job.source_solution.read_text()
    folder = args.output / f"round-{round_number}" / job.mode / f"{job.task}-{job.trial}"
    metadata = {
        "round": round_number,
        "parent_identity": {
            "mode": job.mode,
            "task": job.task,
            "trial": job.trial,
        },
        "parent_workspace": str(job.source_workspace),
        "parent_solution_sha256": sha256(seed.encode()).hexdigest(),
        "source_traces": [str(path) for path in job.source_traces],
    }
    return run_trial(
        job.mode,
        job.task,
        job.trial,
        args,
        folder=folder,
        seed_solution=seed,
        prompt=prompt,
        trial_metadata=metadata,
    )


def run_diagnostic_trial(job: RepairJob, args) -> dict:
    """从冻结最终候选启动一次独立复测；目录与提示均不进入修复账本。"""
    evidence = build_repair_evidence(job.source_row)
    prompt = build_diagnostic_prompt(job, evidence)
    seed = job.source_solution.read_text()
    folder = args.output / "trials" / job.mode / f"{job.task}-{job.trial}"
    metadata = {
        "kind": "independent_stage3_diagnostic",
        "merge_into_baseline": False,
        "parent_identity": {
            "mode": job.mode,
            "task": job.task,
            "trial": job.trial,
        },
        "parent_solution_sha256": sha256(seed.encode()).hexdigest(),
    }
    return run_trial(
        job.mode,
        job.task,
        job.trial,
        args,
        folder=folder,
        seed_solution=seed,
        prompt=prompt,
        trial_metadata=metadata,
        trial_metadata_label="diagnostic",
        prompt_filename="diagnostic-prompt.txt",
    )


def execute_diagnostic_jobs(
    jobs: list[RepairJob],
    args,
    *,
    runner=None,
    expected_count: int = 12,
) -> list[dict]:
    """严格串行执行指定冻结身份，每项恰好一次，避免并发改变来源协议。"""
    if args.workers != 1:
        raise ValueError("独立诊断必须使用 workers=1")
    identities = [job.identity for job in jobs]
    if len(identities) != expected_count or len(set(identities)) != expected_count:
        raise ValueError(f"独立诊断必须恰好包含 {expected_count} 个不重复冻结身份")
    runner = runner or run_diagnostic_trial
    return [runner(job, args) for job in jobs]


def execute_repair_rounds(
    baseline: dict,
    args,
    *,
    runner=None,
) -> list[dict]:
    """按轮并发修复；每个身份成功或传输失败即停止，最多执行两轮。"""
    if args.repair_rounds not in {1, 2}:
        raise ValueError("repair-rounds 必须为 1–2")
    runner = runner or run_repair_round
    active = select_repair_jobs(baseline)
    ledger = {
        job.identity: {
            "identity": {"mode": job.mode, "task": job.task, "trial": job.trial},
            "baseline": job.source_row,
            "rounds": [],
            "final_passed": False,
            "stop_reason": None,
        }
        for job in active
    }
    for round_number in range(1, args.repair_rounds + 1):
        if not active:
            break
        pool = ThreadPoolExecutor(max_workers=args.workers)
        futures = [pool.submit(runner, job, round_number, args) for job in active]
        try:
            results = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            stop_active_trials()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            pool.shutdown()

        next_active = []
        for job, result in zip(active, results, strict=True):
            entry = ledger[job.identity]
            entry["rounds"].append(result)
            entry["final_passed"] = result.get("passed") is True
            if entry["final_passed"]:
                entry["stop_reason"] = "passed"
                continue
            status = result.get("leader_status")
            if status in {"provider_error", "harness_error"}:
                entry["stop_reason"] = status
                continue
            if round_number == args.repair_rounds:
                entry["stop_reason"] = "round_limit"
                continue
            next_active.extend(select_repair_jobs({"trials": [result]}))
        active = next_active
    return list(ledger.values())


def resume_repair_transport(
    baseline: dict,
    source_ledger: list[dict],
    args,
    *,
    runner=None,
) -> list[dict]:
    """只续跑瞬时 provider error，并保留被替代尝试以完整核算成本。"""
    if args.repair_rounds not in {1, 2}:
        raise ValueError("repair-rounds 必须为 1–2")
    runner = runner or run_repair_round
    baseline_identities = {trial_identity(row) for row in baseline.get("trials", [])}
    ledger = copy.deepcopy(source_ledger)
    for entry in ledger:
        identity = trial_identity(entry.get("identity", {}))
        if identity not in baseline_identities:
            raise ValueError("repair ledger 身份不在 baseline")
        rounds = entry.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            raise ValueError("repair ledger 缺少 rounds")
        failed_transport = rounds[-1]
        if not is_transient_provider_error(failed_transport):
            continue

        round_number = len(rounds)
        parent = entry["baseline"] if round_number == 1 else rounds[-2]
        job = select_repair_jobs({"trials": [parent]})[0]
        replacement = runner(job, round_number, args)
        entry.setdefault("superseded_transport_attempts", []).append(failed_transport)
        rounds[-1] = replacement
        entry["final_passed"] = replacement.get("passed") is True
        if entry["final_passed"]:
            entry["stop_reason"] = "passed"
            continue
        status = replacement.get("leader_status")
        if status in {"provider_error", "harness_error"}:
            entry["stop_reason"] = status
            continue
        if round_number >= args.repair_rounds:
            entry["stop_reason"] = "round_limit"
            continue

        next_job = select_repair_jobs({"trials": [replacement]})[0]
        next_result = runner(next_job, round_number + 1, args)
        rounds.append(next_result)
        entry["final_passed"] = next_result.get("passed") is True
        entry["stop_reason"] = (
            "passed" if entry["final_passed"] else next_result.get("leader_status")
        )
        if entry["stop_reason"] not in {"passed", "provider_error", "harness_error"}:
            entry["stop_reason"] = "round_limit"
    return ledger


def stop_active_trials() -> None:
    """批次被用户中断时终止所有独立进程组，避免模型请求成为孤儿进程。"""
    with _PROCESS_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)


def main() -> int:
    """默认只校准；--live 才运行真实模型，恢复批次写入新目录而不覆盖原证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("pi-claw.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="从已有 report.json 仅续跑 provider_error，并重新生成完整合并报告",
    )
    parser.add_argument("--merge-from", type=Path, help="离线合并的完整来源批次")
    parser.add_argument("--replace-from", type=Path, help="离线替换的 trial 子集批次")
    parser.add_argument(
        "--replace-origin",
        type=Path,
        help="替换批次是旧版恢复报告时，提供其原始来源以完整核算失败请求成本",
    )
    parser.add_argument("--repair-from", type=Path, help="从完整报告自动修复失败候选")
    parser.add_argument(
        "--resume-repair-from",
        type=Path,
        help="从已有修复报告只续跑 Trace 证明为瞬时故障的 provider_error",
    )
    parser.add_argument(
        "--diagnostic-from",
        type=Path,
        help="独立复测冻结的 12 个最终失败候选；不生成第三修复轮、不回写 78/90",
    )
    parser.add_argument(
        "--continue-diagnostic-from",
        type=Path,
        help="接续完整诊断或派生报告，只重跑最新视图中仍失败的身份",
    )
    parser.add_argument(
        "--diagnostic-replacements-from",
        type=Path,
        help="首次接续诊断报告时，提供已经完成的部分 grade.json 根目录",
    )
    parser.add_argument("--expected-diagnostic-failures", type=int)
    parser.add_argument("--expected-diagnostic-replacements", type=int)
    parser.add_argument("--repair-rounds", type=int, default=2, help="每个候选最多修复 1–2 轮")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=420)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="每个普通模型逻辑请求的最大 HTTP 尝试次数；profile 可单独收紧",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=2.0,
        help="暂时性失败后的指数退避基数秒数",
    )
    parser.add_argument(
        "--max-retry-delay",
        type=float,
        default=10.0,
        help="单次重试等待上限秒数，也用于限制 Retry-After",
    )
    parser.add_argument("--task", choices=tuple(TASKS), action="append")
    parser.add_argument("--mode", choices=MODES, action="append")
    parser.add_argument("--worker", choices=tuple(TASKS), help=argparse.SUPPRESS)
    parser.add_argument("--worker-prompt-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 10:
        parser.error("max-attempts 为 1–10")
    if not 0 <= args.retry_base_delay <= args.max_retry_delay <= 60:
        parser.error("重试等待须满足 0 <= retry-base-delay <= max-retry-delay <= 60")
    if args.worker:
        args.mode = args.mode[0]
        worker(args)
        return 0
    continuation_requested = any(
        value is not None
        for value in (
            args.continue_diagnostic_from,
            args.diagnostic_replacements_from,
            args.expected_diagnostic_failures,
            args.expected_diagnostic_replacements,
        )
    )
    if continuation_requested:
        if not args.live:
            parser.error("诊断续跑必须与 --live 一起使用")
        if args.continue_diagnostic_from is None or args.expected_diagnostic_failures is None:
            parser.error("诊断续跑必须提供来源和期望失败数量")
        replacement_pair = (
            args.diagnostic_replacements_from is not None,
            args.expected_diagnostic_replacements is not None,
        )
        if replacement_pair[0] != replacement_pair[1]:
            parser.error("部分替换目录和期望替换数量必须同时提供或同时省略")
        competing = (
            args.diagnostic_from,
            args.resume_from,
            args.merge_from,
            args.replace_from,
            args.replace_origin,
            args.repair_from,
            args.resume_repair_from,
            args.mode,
            args.task,
        )
        if any(value is not None for value in competing):
            parser.error("诊断续跑不能与普通诊断、续跑、合并、修复或身份筛选混用")
        if args.expected_diagnostic_failures < 1 or (
            args.expected_diagnostic_replacements is not None
            and args.expected_diagnostic_replacements < 1
        ):
            parser.error("诊断续跑的期望数量必须为正整数")
        try:
            summary = run_diagnostic_continuation(
                args.continue_diagnostic_from,
                args.diagnostic_replacements_from,
                args,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法执行诊断续跑：{error}")
        print(json.dumps(summary["continuation"], ensure_ascii=False), flush=True)
        return 0 if summary["continuation"]["derived_failures"] == 0 else 1
    if args.diagnostic_from:
        if not args.live:
            parser.error("--diagnostic-from 必须与 --live 一起使用")
        competing = (
            args.resume_from,
            args.merge_from,
            args.replace_from,
            args.replace_origin,
            args.repair_from,
            args.resume_repair_from,
            args.mode,
            args.task,
        )
        if any(value is not None for value in competing):
            parser.error("独立诊断不能与续跑、合并、修复、--mode 或 --task 混用")
        try:
            summary = run_diagnostic_report(args.diagnostic_from, args)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法执行独立诊断：{error}")
        diagnostic = summary["diagnostic"]
        print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
        return (
            0
            if diagnostic["deadline_or_loop_target_met"]
            and diagnostic["provider_classification_target_met"]
            else 1
        )
    merge_requested = any(
        value is not None for value in (args.merge_from, args.replace_from, args.replace_origin)
    )
    if merge_requested:
        if args.merge_from is None or args.replace_from is None:
            parser.error("--merge-from 与 --replace-from 必须同时提供")
        if (
            args.live
            or args.resume_from
            or args.repair_from
            or args.resume_repair_from
            or args.mode
            or args.task
        ):
            parser.error(
                "离线合并不能同时使用 --live、--resume-from、--repair-from、--mode 或 --task"
            )
        try:
            summary = merge_report_roots(
                args.merge_from,
                args.replace_from,
                args.output,
                replacement_origin_root=args.replace_origin,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法合并报告：{error}")
        print(json.dumps(summary["by_mode"], ensure_ascii=False), flush=True)
        return 0 if all(row["passed"] for row in summary["trials"]) else 1
    if args.repair_from:
        if not args.live:
            parser.error("--repair-from 必须与 --live 一起使用")
        if args.resume_from or args.resume_repair_from or args.mode or args.task:
            parser.error(
                "修复模式从来源报告恢复，不能同时指定 --resume-from、"
                "--resume-repair-from、--mode 或 --task"
            )
        if not 1 <= args.workers <= 3:
            parser.error("workers 为 1–3")
        if args.repair_rounds not in {1, 2}:
            parser.error("repair-rounds 必须为 1–2")
        try:
            summary = run_repair_report(args.repair_from, args)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法执行修复批次：{error}")
        print(json.dumps(summary["repair"], ensure_ascii=False), flush=True)
        return 0 if summary["repair"]["post_repair"]["strict_failures"] == 0 else 1
    if args.resume_repair_from:
        if not args.live:
            parser.error("--resume-repair-from 必须与 --live 一起使用")
        if args.resume_from or args.mode or args.task:
            parser.error("修复续跑不能同时指定 --resume-from、--mode 或 --task")
        if not 1 <= args.workers <= 3:
            parser.error("workers 为 1–3")
        try:
            summary = run_repair_continuation(args.resume_repair_from, args)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法执行修复续跑：{error}")
        print(json.dumps(summary["repair"], ensure_ascii=False), flush=True)
        return 0 if summary["repair"]["post_repair"]["strict_failures"] == 0 else 1
    if not 1 <= args.trials <= 5 or not 1 <= args.workers <= 3:
        parser.error("trials 为 1–5；workers 为 1–3")
    if not 30 <= args.deadline <= 900 or not 4 <= args.max_steps <= 40:
        parser.error("deadline 为 30–900 秒；max-steps 为 4–40")
    source_report = None
    resume_jobs = None
    if args.resume_from:
        if not args.live:
            parser.error("--resume-from 必须与 --live 一起使用")
        if args.mode or args.task:
            parser.error("续跑模式从来源报告恢复，不能同时指定 --mode 或 --task")
        resume_root = args.resume_from.resolve()
        try:
            source_report = json.loads((resume_root / "report.json").read_text())
            resume_jobs = select_resume_jobs(source_report)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            parser.error(f"无法读取续跑来源：{error}")
        if not resume_jobs:
            parser.error("续跑来源没有 provider_error trial")
        modes = list(source_report["modes"])
        tasks = list(source_report["tasks"])
        args.trials = int(source_report["trials_per_task"])
        args.deadline = int(source_report["deadline_seconds"])
        args.max_steps = int(source_report["max_steps"])
    else:
        modes = list(dict.fromkeys(args.mode or MODES))
        tasks = list(dict.fromkeys(args.task or TASKS))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = calibrate()
    save_json(args.output / "grader-calibration.json", calibration)
    if not calibration["passed"]:
        raise RuntimeError("评分器校准失败，停止真实模型请求")
    print(f"[校准] {len(TASKS)} 项参考实现通过，初始缺陷均被隐藏用例检出。", flush=True)
    if not args.live:
        print("未指定 --live：未调用模型。", flush=True)
        return 0
    settings = load_settings(config_path=args.config.resolve(), cwd=args.config.resolve().parent)
    roles = {settings.workflow.planner, settings.workflow.coder, settings.workflow.reviewer}
    if not settings.workflow.enabled or not roles <= set(settings.profiles):
        parser.error("比较需要已启用且模型完整的 planner/coder/reviewer 工作流配置")
    fingerprint = _code_fingerprint()
    premium_profile = settings.workflow.premium_reviewer
    premium_model = settings.profiles[premium_profile].model if premium_profile else None
    models = {
        role: settings.profiles[getattr(settings.workflow, role)].model
        for role in ("planner", "coder", "reviewer")
    }
    if source_report and (
        source_report.get("models") != models or source_report.get("premium_model") != premium_model
    ):
        parser.error("续跑必须沿用来源报告中的 DeepSeek/Kimi 模型分配")
    manifest = {
        "schema_version": 3,
        "grader_version": GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_model": True,
        "network_used": True,
        "source_sha256": fingerprint,
        "modes": modes,
        "tasks": tasks,
        "trials_per_task": args.trials,
        "workers": args.workers,
        "deadline_seconds": args.deadline,
        "max_steps": args.max_steps,
        "transport": {
            "max_attempts_per_request": args.max_attempts,
            "retry_base_delay_seconds": args.retry_base_delay,
            "max_retry_delay_seconds": args.max_retry_delay,
        },
        "models": models,
        "premium_model": premium_model,
        "accounting": "Token 统计包含主 Agent、全部子 Agent、工作流阶段与返工；不计算货币费用",
    }
    if source_report:
        manifest["continuation"] = {
            "selected_status": "provider_error",
            "requested_trials": len(resume_jobs),
            "source_grader_version": source_report.get("grader_version"),
            "source_sha256": source_report.get("source_sha256"),
            "initial_workers": source_report.get("workers"),
            "continuation_workers": args.workers,
            "retained_trials_regraded": len(source_report["trials"]) - len(resume_jobs),
        }
    save_json(args.output / "manifest.json", manifest)
    jobs = resume_jobs or [
        (mode, task_id, number)
        for task_id in tasks
        for number in range(1, args.trials + 1)
        for mode in modes
    ]
    print(
        f"[{'续跑' if source_report else '批次'}] {len(jobs)} trials；modes={modes}；"
        f"tasks={tasks}；并发={args.workers}；"
        f"每 trial 期限={args.deadline}s；每请求最多 {args.max_attempts} 次尝试，"
        f"退避基数={args.retry_base_delay}s。",
        flush=True,
    )
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = [
        pool.submit(run_trial, mode, task_id, number, args) for mode, task_id, number in jobs
    ]
    try:
        reports = [future.result() for future in futures]
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        stop_active_trials()
        pool.shutdown(wait=True, cancel_futures=True)
        print("[中断] 已终止运行中的 trial 进程组；未生成汇总报告。", flush=True)
        return 130
    else:
        pool.shutdown()
    if source_report:
        reports = combine_resumed_reports(source_report, reports)
        manifest["continuation"]["remaining_provider_errors"] = sum(
            row["leader_status"] == "provider_error" for row in reports
        )
        manifest["acquisition"] = build_continuation_acquisition(
            source_report,
            {"trials": reports},
        )
    else:
        manifest["acquisition"] = _report_acquisition({"trials": reports})
    by_mode = {
        mode: summarize(
            [row for row in reports if row["mode"] == mode], premium_model=premium_model
        )
        for mode in modes
    }
    by_mode_and_task = {
        mode: {
            task_id: summarize(
                [row for row in reports if row["mode"] == mode and row["task"] == task_id],
                premium_model=premium_model,
            )
            for task_id in tasks
        }
        for mode in modes
    }
    summary = {
        **manifest,
        "by_mode": by_mode,
        "by_mode_and_task": by_mode_and_task,
        "trials": reports,
    }
    save_json(args.output / "manifest.json", manifest)
    save_json(args.output / "report.json", summary)
    save_json(args.output / "public-summary.json", build_public_summary(summary))
    print(json.dumps(summary["by_mode"], ensure_ascii=False), flush=True)
    return 0 if all(row["passed"] for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
