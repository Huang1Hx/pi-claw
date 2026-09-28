"""版本化、完全离线的 Coding Agent 评测基线。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path, PurePosixPath
from threading import Lock
from time import perf_counter
from typing import Any

from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.schemas import ModelReply, RunStatus, ToolCall
from persistence.trace import TraceReplay, TraceWriter, redact
from providers import ProviderError, ScriptedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


@dataclass(frozen=True, slots=True)
class EvalAssertion:
    """一条可解释的宿主断言。"""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """单个独立工作区的评测结果。"""

    case_id: str
    category: str
    description: str
    passed: bool
    run_status: str
    steps: int
    tool_calls: int
    successful_tool_calls: int
    non_ok_tool_results: int
    policy_denials: int
    approval_denials: int
    completion_rejections: int
    completion_verified: bool
    duration_ms: float
    isolation_id: str
    trace_path: str
    assertions: tuple[EvalAssertion, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的数据对象。"""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvalReport:
    """完整离线评测报告及聚合指标。"""

    schema_version: int
    suite_name: str
    suite_version: int
    generated_at: str
    provider: str
    network_used: bool
    metrics: dict[str, Any]
    cases: tuple[EvalCaseResult, ...]

    @property
    def passed(self) -> bool:
        """所有非空评测 case 均通过时返回 True。"""
        return bool(self.cases) and all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的数据对象。"""
        return {
            "schema_version": self.schema_version,
            "suite_name": self.suite_name,
            "suite_version": self.suite_version,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "network_used": self.network_used,
            "metrics": self.metrics,
            "cases": [case.to_dict() for case in self.cases],
        }


def default_suite_path() -> Path:
    """返回随 Python 包版本化的默认离线评测集。"""

    resource = files("evaluation").joinpath("cases.json")
    return Path(str(resource))


def load_eval_suite(path: str | Path | None = None) -> dict[str, Any]:
    """加载版本化用例，拒绝未知格式、不安全 fixture 和重复 case id。"""
    suite_path = Path(path) if path is not None else default_suite_path()
    try:
        value = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"评测集不是有效 JSON：{suite_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("评测集根节点必须是 JSON 对象")
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ValueError("只支持 schema_version=1 的评测集")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("评测集 name 必须是非空字符串")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("评测集 version 必须是正整数")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集 cases 必须是非空数组")
    seen: set[str] = set()
    for raw_case in cases:
        _validate_case(raw_case, seen)
    return value


class EvalRunner:
    """使用真实 Agent/Policy/Tool/Trace 链路运行确定性任务集。"""

    def __init__(
        self,
        suite: dict[str, Any],
        *,
        traces_dir: str | Path,
    ) -> None:
        """绑定已加载的评测集并创建轨迹目录；每个 case 的工作区在执行时单独创建。"""
        self.suite = suite
        self.traces_dir = Path(traces_dir).expanduser().resolve(strict=False)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        *,
        case_ids: set[str] | None = None,
        workers: int = 1,
    ) -> EvalReport:
        """按所选 case 在本机执行离线评测，返回 EvalReport。

        case_ids 为空时运行全部用例；workers 决定线程数，最多不超过选中 case 数。
        每个 case 使用独立工作区、Agent 和 Trace，结果按原用例顺序汇总。
        这里是本机线程并发，不是分布式执行，也不度量真实 LLM 的任务质量。
        """
        if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
            raise ValueError("workers 必须是正整数")
        available = {str(case["id"]) for case in self.suite["cases"]}
        if case_ids:
            unknown = sorted(case_ids - available)
            if unknown:
                raise ValueError(f"未知评测 case：{', '.join(unknown)}")
        selected = [
            case for case in self.suite["cases"] if not case_ids or str(case["id"]) in case_ids
        ]
        workers_used = min(workers, len(selected))
        active_cases = 0
        max_parallel_cases = 0
        counter_lock = Lock()

        def run_tracked(case: dict[str, Any]) -> EvalCaseResult:
            """只在修改并发计数时持线程锁；真实 case 在锁外执行，异常时也回收计数。"""
            nonlocal active_cases, max_parallel_cases
            with counter_lock:
                active_cases += 1
                max_parallel_cases = max(max_parallel_cases, active_cases)
            try:
                return self._run_case(case)
            finally:
                with counter_lock:
                    active_cases -= 1

        started = perf_counter()
        if workers_used == 1:
            results = tuple(run_tracked(case) for case in selected)
        else:
            with ThreadPoolExecutor(
                max_workers=workers_used,
                thread_name_prefix="pi-claw-eval",
            ) as executor:
                # map 保持输入顺序；即便任务完成先后不同，报告仍能稳定逐项比较。
                results = tuple(executor.map(run_tracked, selected))
        wall_duration_ms = round((perf_counter() - started) * 1000, 3)
        metrics = self._aggregate(
            results,
            workers_requested=workers,
            workers_used=workers_used,
            max_parallel_cases=max_parallel_cases,
            wall_duration_ms=wall_duration_ms,
        )
        return EvalReport(
            schema_version=1,
            suite_name=str(self.suite["name"]),
            suite_version=int(self.suite["version"]),
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            provider="scripted",
            network_used=False,
            metrics=metrics,
            cases=results,
        )

    def _run_case(self, raw_case: dict[str, Any]) -> EvalCaseResult:
        """在独立临时工作区运行正式 Agent 链路，再据文件和轨迹验收；同名旧轨迹会替换。"""
        case_id = str(raw_case["id"])
        trace_path = self.traces_dir / f"{case_id}.jsonl"
        if trace_path.exists():
            trace_path.unlink()
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"pi-claw-eval-{case_id}-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            isolation_id = sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
            self._materialize_files(workspace, raw_case.get("files", {}))
            self._materialize_files(root, raw_case.get("outside_files", {}))
            policy_config = raw_case.get("policy", {})
            auto_approve = bool(policy_config.get("auto_approve", False))
            policy = WorkspacePolicy(
                workspace,
                allow_write=bool(policy_config.get("allow_write", False)),
                allow_commands=bool(policy_config.get("allow_commands", False)),
                command_profile=str(policy_config.get("command_profile", "minimal")),
            )
            executor = ToolExecutor(
                policy,
                approval_callback=(lambda *_: True) if auto_approve else None,
            )
            result = Agent(
                _ConditionedScriptedProvider(raw_case["replies"]),
                executor,
                trace=TraceWriter(trace_path),
                max_steps=int(raw_case.get("max_steps", 8)),
                completion_verifier=ToolEvidenceCompletionVerifier(),
            ).run(str(raw_case["task"]))
            events = TraceReplay(trace_path).read()
            assertions = tuple(
                self._evaluate_expectations(
                    raw_case["expect"],
                    workspace,
                    root,
                    result.status,
                    events,
                )
            )
        duration_ms = round((perf_counter() - started) * 1000, 3)
        tool_results = [event["payload"] for event in events if event["event"] == "tool_result"]
        policy_denials = sum(
            str(item.get("error", "")).startswith("策略拒绝") for item in tool_results
        )
        approval_denials = sum("未获批准" in str(item.get("error", "")) for item in tool_results)
        successful = sum(bool(item.get("ok")) for item in tool_results)
        completion_rejections = sum(event["event"] == "completion_rejected" for event in events)
        completion_verified = any(event["event"] == "completion_verified" for event in events)
        return EvalCaseResult(
            case_id=case_id,
            category=str(raw_case["category"]),
            description=str(raw_case["description"]),
            passed=all(assertion.passed for assertion in assertions),
            run_status=result.status.value,
            steps=result.steps,
            tool_calls=len(tool_results),
            successful_tool_calls=successful,
            non_ok_tool_results=len(tool_results) - successful,
            policy_denials=policy_denials,
            approval_denials=approval_denials,
            completion_rejections=completion_rejections,
            completion_verified=completion_verified,
            duration_ms=duration_ms,
            isolation_id=isolation_id,
            trace_path=str(trace_path),
            assertions=assertions,
        )

    @staticmethod
    def _materialize_files(root: Path, raw_files: dict[str, Any]) -> None:
        """将用例定义的文本写入指定临时根目录；路径须是经校验的安全相对路径。"""
        if not isinstance(raw_files, dict):
            raise ValueError("case files 必须是对象")
        for name, content in raw_files.items():
            relative = _safe_fixture_path(name)
            if not isinstance(content, str):
                raise ValueError(f"fixture 内容必须是字符串：{name}")
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    @staticmethod
    def _parse_reply(raw: dict[str, Any]) -> ModelReply:
        """把用例 JSON 转成 ModelReply，并将受支持的解释器占位符替换为当前 Python。"""
        if not isinstance(raw, dict):
            raise ValueError("replies 中每项必须是对象")
        if "final_text" in raw:
            if not isinstance(raw["final_text"], str):
                raise ValueError("final_text reply 格式无效")
            return ModelReply.final(raw["final_text"])
        calls = raw.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            raise ValueError("reply 必须包含 final_text 或非空 tool_calls")
        parsed: list[ToolCall] = []
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("tool_calls 中每项必须是对象")
            arguments = _replace_placeholders(call.get("arguments", {}))
            parsed.append(
                ToolCall(
                    str(call["id"]),
                    str(call["name"]),
                    arguments,
                )
            )
        return ModelReply.calls(parsed)

    @staticmethod
    def _evaluate_expectations(
        expected: dict[str, Any],
        workspace: Path,
        root: Path,
        status: RunStatus,
        events: list[dict[str, Any]],
    ):
        """逐项产出终态、事件、工具结果和文件内容断言；不把模型自述当作通过证据。"""
        expected_status = str(expected["status"])
        yield EvalAssertion(
            "run_status",
            status.value == expected_status,
            f"expected={expected_status}, actual={status.value}",
        )
        event_names = [str(event.get("event")) for event in events]
        for event_name in expected.get("required_events", []):
            yield EvalAssertion(
                f"event:{event_name}",
                event_name in event_names,
                f"event {event_name} {'present' if event_name in event_names else 'missing'}",
            )
        actual_results = [event["payload"] for event in events if event["event"] == "tool_result"]
        expected_results = expected.get("tool_results", [])
        yield EvalAssertion(
            "tool_result_count",
            len(actual_results) == len(expected_results),
            f"expected={len(expected_results)}, actual={len(actual_results)}",
        )
        for index, wanted in enumerate(expected_results):
            actual = actual_results[index] if index < len(actual_results) else {}
            tool = str(wanted["tool"])
            ok = bool(wanted["ok"])
            matches = actual.get("tool_name") == tool and actual.get("ok") is ok
            error_contains = wanted.get("error_contains")
            if error_contains is not None:
                matches = matches and str(error_contains) in str(actual.get("error", ""))
            output_contains = wanted.get("output_contains")
            if output_contains is not None:
                encoded_output = json.dumps(actual.get("output"), ensure_ascii=False)
                matches = matches and str(output_contains) in encoded_output
            yield EvalAssertion(
                f"tool_result:{index}:{tool}",
                matches,
                _tool_assertion_detail(tool, ok, actual),
            )
        for name, text in expected.get("file_contains", {}).items():
            target = workspace.joinpath(*_safe_fixture_path(name).parts)
            actual = target.read_text(encoding="utf-8") if target.is_file() else ""
            yield EvalAssertion(
                f"file_contains:{name}",
                str(text) in actual,
                f"expected substring present={str(text) in actual}",
            )
        for name, text in expected.get("file_equals", {}).items():
            target = workspace.joinpath(*_safe_fixture_path(name).parts)
            actual = target.read_text(encoding="utf-8") if target.is_file() else None
            yield EvalAssertion(
                f"file_equals:{name}",
                actual == text,
                f"expected exact content match={actual == text}",
            )
        for name, text in expected.get("outside_file_equals", {}).items():
            target = root.joinpath(*_safe_fixture_path(name).parts)
            actual = target.read_text(encoding="utf-8") if target.is_file() else None
            yield EvalAssertion(
                f"outside_file_equals:{name}",
                actual == text,
                f"outside boundary unchanged={actual == text}",
            )

    @staticmethod
    def _aggregate(
        results: tuple[EvalCaseResult, ...],
        *,
        workers_requested: int,
        workers_used: int,
        max_parallel_cases: int,
        wall_duration_ms: float,
    ) -> dict[str, Any]:
        """汇总用例通过率、安全拒绝、耗时及隔离指标；并发加速仅是本机观测值。"""
        total = len(results)
        passed = sum(case.passed for case in results)
        security = [case for case in results if case.category == "security"]
        status_counts = Counter(case.run_status for case in results)
        tool_calls = sum(case.tool_calls for case in results)
        successful = sum(case.successful_tool_calls for case in results)
        serial_work_ms = round(sum(case.duration_ms for case in results), 3)
        observed_speedup = serial_work_ms / wall_duration_ms if wall_duration_ms else 0.0
        isolation_ids = [case.isolation_id for case in results]
        trace_paths = [case.trace_path for case in results]
        isolation_violations = (total - len(set(isolation_ids))) + (total - len(set(trace_paths)))
        return {
            "total_cases": total,
            "passed_cases": passed,
            "failed_cases": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "security_cases": len(security),
            "security_pass_rate": (
                round(sum(case.passed for case in security) / len(security), 4)
                if security
                else None
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "total_tool_calls": tool_calls,
            "successful_tool_calls": successful,
            "non_ok_tool_results": tool_calls - successful,
            "assertion_failures": sum(
                not assertion.passed for case in results for assertion in case.assertions
            ),
            "security_violations": sum(not case.passed for case in security),
            "policy_denials": sum(case.policy_denials for case in results),
            "approval_denials": sum(case.approval_denials for case in results),
            "completion_rejections": sum(case.completion_rejections for case in results),
            "verified_completions": sum(case.completion_verified for case in results),
            "unverified_runs": sum(case.run_status == RunStatus.UNVERIFIED for case in results),
            "mean_steps": round(sum(case.steps for case in results) / total, 3) if total else 0.0,
            "execution_backend": "thread",
            "workers_requested": workers_requested,
            "workers_used": workers_used,
            "max_parallel_cases": max_parallel_cases,
            "wall_duration_ms": wall_duration_ms,
            "serial_work_ms": serial_work_ms,
            "observed_speedup": round(observed_speedup, 4),
            "parallel_efficiency": (
                round(observed_speedup / workers_used, 4) if workers_used else 0.0
            ),
            "task_isolation_violations": isolation_violations,
            # 兼容 v1 报告字段：其含义一直是各 case duration 之和，而非墙钟时间。
            "duration_ms": serial_work_ms,
        }


def write_eval_report(report: EvalReport, path: str | Path) -> Path:
    """原子写入脱敏 JSON 报告，避免中断留下半份指标文件。"""

    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(redact(report.to_dict()), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}-",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return target


def _validate_case(raw_case: Any, seen: set[str]) -> None:
    """校验 case 身份、脚本、预算和预期，并更新已见 ID 集合以拒绝重名用例。"""
    if not isinstance(raw_case, dict):
        raise ValueError("每个评测 case 必须是对象")
    case_id = raw_case.get("id")
    if not isinstance(case_id, str) or not case_id or not case_id.replace("_", "").isalnum():
        raise ValueError("case id 只能包含字母、数字和下划线")
    if case_id in seen:
        raise ValueError(f"case id 重复：{case_id}")
    seen.add(case_id)
    for key in ("category", "description", "task"):
        if not isinstance(raw_case.get(key), str) or not raw_case[key].strip():
            raise ValueError(f"case {case_id} 的 {key} 必须是非空字符串")
    if not isinstance(raw_case.get("replies"), list) or not raw_case["replies"]:
        raise ValueError(f"case {case_id} replies 必须是非空数组")
    for reply in raw_case["replies"]:
        _validate_reply(case_id, reply)
    max_steps = raw_case.get("max_steps", 8)
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps <= 0:
        raise ValueError(f"case {case_id} max_steps 必须是正整数")
    policy = raw_case.get("policy", {})
    if not isinstance(policy, dict):
        raise ValueError(f"case {case_id} policy 必须是对象")
    for flag in ("allow_write", "allow_commands", "auto_approve"):
        if flag in policy and not isinstance(policy[flag], bool):
            raise ValueError(f"case {case_id} policy.{flag} 必须是布尔值")
    if policy.get("command_profile", "minimal") not in {"minimal", "coding"}:
        raise ValueError(f"case {case_id} policy.command_profile 无效")
    expected = raw_case.get("expect")
    if not isinstance(expected, dict) or expected.get("status") not in {
        status.value for status in RunStatus
    }:
        raise ValueError(f"case {case_id} expect.status 无效")
    _validate_expectations(case_id, expected)
    for files_key in ("files", "outside_files"):
        raw_files = raw_case.get(files_key, {})
        if not isinstance(raw_files, dict):
            raise ValueError(f"case {case_id} {files_key} 必须是对象")
        for name in raw_files:
            _safe_fixture_path(name)
        if not all(isinstance(content, str) for content in raw_files.values()):
            raise ValueError(f"case {case_id} {files_key} 内容必须是字符串")


def _validate_reply(case_id: str, reply: Any) -> None:
    """在执行前检查预设回复及工具反馈前置条件的结构，拒绝歧义或畸形脚本。"""
    if not isinstance(reply, dict):
        raise ValueError(f"case {case_id} reply 必须是对象")
    if "final_text" in reply:
        if set(reply) - {"final_text", "require_last_tool_result"} or not isinstance(
            reply["final_text"], str
        ):
            raise ValueError(f"case {case_id} final_text reply 格式无效")
    elif set(reply) - {"tool_calls", "require_last_tool_result"}:
        raise ValueError(f"case {case_id} reply 必须只包含 final_text 或 tool_calls")
    else:
        calls = reply.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            raise ValueError(f"case {case_id} tool_calls 必须是非空数组")
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError(f"case {case_id} ToolCall 必须是对象")
            if not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError(f"case {case_id} ToolCall.id 必须是非空字符串")
            if not isinstance(call.get("name"), str) or not call["name"]:
                raise ValueError(f"case {case_id} ToolCall.name 必须是非空字符串")
            if not isinstance(call.get("arguments", {}), dict):
                raise ValueError(f"case {case_id} ToolCall.arguments 必须是对象")
    condition = reply.get("require_last_tool_result")
    if condition is not None:
        if not isinstance(condition, dict):
            raise ValueError(f"case {case_id} require_last_tool_result 必须是对象")
        if not isinstance(condition.get("tool"), str) or not condition["tool"]:
            raise ValueError(f"case {case_id} require_last_tool_result.tool 无效")
        if not isinstance(condition.get("ok"), bool):
            raise ValueError(f"case {case_id} require_last_tool_result.ok 必须是布尔值")
        if "error_contains" in condition and not isinstance(condition["error_contains"], str):
            raise ValueError(f"case {case_id} require_last_tool_result.error_contains 无效")
        if "output_contains" in condition and not isinstance(condition["output_contains"], str):
            raise ValueError(f"case {case_id} require_last_tool_result.output_contains 无效")


def _validate_expectations(case_id: str, expected: dict[str, Any]) -> None:
    """校验事件、工具结果和文件断言的类型与路径，避免无效预期被误判为通过。"""
    events = expected.get("required_events", [])
    if not isinstance(events, list) or not all(
        isinstance(event, str) and event for event in events
    ):
        raise ValueError(f"case {case_id} required_events 必须是字符串数组")
    tool_results = expected.get("tool_results", [])
    if not isinstance(tool_results, list):
        raise ValueError(f"case {case_id} tool_results 必须是数组")
    for result in tool_results:
        if not isinstance(result, dict):
            raise ValueError(f"case {case_id} expected ToolResult 必须是对象")
        if not isinstance(result.get("tool"), str) or not result["tool"]:
            raise ValueError(f"case {case_id} expected ToolResult.tool 无效")
        if not isinstance(result.get("ok"), bool):
            raise ValueError(f"case {case_id} expected ToolResult.ok 必须是布尔值")
        if "error_contains" in result and not isinstance(result["error_contains"], str):
            raise ValueError(f"case {case_id} error_contains 必须是字符串")
        if "output_contains" in result and not isinstance(result["output_contains"], str):
            raise ValueError(f"case {case_id} output_contains 必须是字符串")
    for mapping_name in ("file_contains", "file_equals", "outside_file_equals"):
        mapping = expected.get(mapping_name, {})
        if not isinstance(mapping, dict):
            raise ValueError(f"case {case_id} {mapping_name} 必须是对象")
        for name, content in mapping.items():
            _safe_fixture_path(name)
            if not isinstance(content, str):
                raise ValueError(f"case {case_id} {mapping_name} 内容必须是字符串")


def _safe_fixture_path(value: Any) -> PurePosixPath:
    """将夹具路径限定为非空相对路径，拒绝绝对路径和父目录穿越。"""
    if not isinstance(value, str) or not value:
        raise ValueError("fixture 路径必须是非空字符串")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"fixture 路径必须是安全相对路径：{value}")
    return path


def _replace_placeholders(value: Any) -> Any:
    """递归替换精确的 {{PYTHON}} 占位符；不执行 shell 或展开任意模板。"""
    if isinstance(value, str):
        return sys.executable if value == "{{PYTHON}}" else value
    if isinstance(value, list):
        return [_replace_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _replace_placeholders(item) for key, item in value.items()}
    return value


def _tool_assertion_detail(tool: str, ok: bool, actual: Any) -> str:
    """格式化工具断言的预期与实测摘要；错误信息先脱敏再截断。"""
    if not isinstance(actual, dict):
        return f"expected tool={tool}, ok={ok}; actual=missing"
    error = redact(str(actual.get("error", "")))
    if len(error) > 300:
        error = error[:286] + "...[TRUNCATED]"
    return (
        f"expected tool={tool}, ok={ok}; actual tool={actual.get('tool_name')}, "
        f"ok={actual.get('ok')}, error={error or 'none'}"
    )


class _ConditionedScriptedProvider:
    """在返回固定动作前，验证上一条 ToolResult 确实进入模型上下文。"""

    def __init__(self, raw_replies: list[dict[str, Any]]) -> None:
        """保留原始脚本与反馈条件，并创建按顺序消费的离线 Provider。"""
        self._raw_replies = tuple(raw_replies)
        self._provider = ScriptedProvider(EvalRunner._parse_reply(reply) for reply in raw_replies)
        self._index = 0

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """消费上下文并返回一次完整的模型回复。"""
        if self._index >= len(self._raw_replies):
            raise ProviderError("评测脚本已耗尽，Agent 尚未到达预期终态")
        raw = self._raw_replies[self._index]
        condition = raw.get("require_last_tool_result")
        if condition is not None:
            self._assert_last_tool_result(messages, condition)
        reply = self._provider.complete(messages, tools)
        self._index += 1
        return reply

    @staticmethod
    def _assert_last_tool_result(
        messages: Sequence[dict[str, Any]],
        condition: dict[str, Any],
    ) -> None:
        """核对最新工具反馈的名称、成功标志及指定内容；不匹配则中止该脚本步骤。"""
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            raise ProviderError("评测前置条件失败：上下文缺少 ToolResult")
        try:
            result = json.loads(str(tool_messages[-1].get("content", "")))
        except json.JSONDecodeError as exc:
            raise ProviderError("评测前置条件失败：ToolResult 不是有效 JSON") from exc
        expected_tool = condition["tool"]
        expected_ok = condition["ok"]
        matches = result.get("tool_name") == expected_tool and result.get("ok") is expected_ok
        error_contains = condition.get("error_contains")
        if error_contains is not None:
            matches = matches and str(error_contains) in str(result.get("error", ""))
        output_contains = condition.get("output_contains")
        if output_contains is not None:
            encoded_output = json.dumps(result.get("output"), ensure_ascii=False)
            matches = matches and str(output_contains) in encoded_output
        if not matches:
            raise ProviderError(
                "评测前置条件失败：上一条 ToolResult 与预期不一致，"
                f"expected tool={expected_tool}, ok={expected_ok}"
            )
