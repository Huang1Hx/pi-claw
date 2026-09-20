"""固定分类/执行替身下的分流对照；验证工程边界与调用分布，不假设模型价格。"""

import json
import math
import sys
import tempfile
import time
from pathlib import Path

from config import ModelProfile, RoutingSettings, Settings
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.schemas import ModelReply, ToolCall
from core.usage import TokenUsage
from persistence.trace import TraceWriter
from providers import ScriptedProvider
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def _reply(calls=()) -> ModelReply:
    """生成带固定 Token 计数的离线回复，仅用于可复现的路由工程测试。"""
    return ModelReply(
        tool_calls=tuple(calls),
        final_text=None if calls else "离线证据已收集",
        usage=TokenUsage(100, 20, 120),
    )


def evaluate_routing(output: Path) -> dict:
    """在独立临时工作区比较轻量分类路由与全主力，报告调用分布和宿主断言。"""
    config = Settings(
        {
            "economy": ModelProfile(
                "economy", "https://gateway.invalid", "fixture-small", "fake-shared"
            ),
            "primary": ModelProfile(
                "primary", "https://gateway.invalid", "fixture-large", "fake-shared"
            ),
        },
        RoutingSettings(intent_strategy="model"),
    )
    scenarios = [
        ("list", "列出项目文件", "economy", "list_files", {"path": "."}, None),
        ("read", "读取alpha.txt", "economy", "read_file", {"path": "alpha.txt"}, None),
        (
            "check",
            "列出项目文件并运行测试",
            "primary",
            "run_command",
            {"argv": [sys.executable, "-m", "unittest", "-q"]},
            None,
        ),
        ("ambiguous", "继续", "primary", "list_files", {"path": "."}, None),
        ("side_effect", "列出项目文件", "economy", "list_files", {"path": "."}, "write"),
        ("failed_read", "读取alpha.txt", "economy", "read_file", {"path": "alpha.txt"}, "missing"),
    ]
    rows = []
    for case_id, task, expected, tool, arguments, deviation in scenarios:
        row = {"case_id": case_id, "expected_initial_profile": expected}
        for mode in ("auto", "primary"):
            with tempfile.TemporaryDirectory(prefix="pi-claw-routing-") as directory:
                root = Path(directory)
                (root / "alpha.txt").write_text("routing evidence\n", encoding="utf-8")
                (root / "test_sample.py").write_text(
                    "import unittest\nclass Check(unittest.TestCase):\n"
                    "    def test_ok(self):\n        self.assertTrue(True)\n",
                    encoding="utf-8",
                )
                normal = [_reply([ToolCall("evidence", tool, arguments)]), _reply()]
                small = normal
                if deviation == "write":
                    small = [
                        _reply(
                            [
                                ToolCall(
                                    "bad-write",
                                    "write_or_replace",
                                    {"path": "unexpected.txt", "old": "", "new": "bad"},
                                )
                            ]
                        )
                    ]
                elif deviation == "missing":
                    small = [_reply([ToolCall("bad-read", "read_file", {"path": "missing.txt"})])]
                intent = ModelReply(
                    final_text=json.dumps(
                        {"complexity": "simple" if expected == "economy" else "complex"}
                    ),
                    usage=TokenUsage(50, 5, 55),
                )
                providers = {
                    "economy": ScriptedProvider([intent, *small]),
                    "primary": ScriptedProvider(normal),
                }
                router = RoutedProvider(
                    config, mode=mode, factory=lambda p, pool=providers: pool[p.name]
                )
                events = []
                trace = TraceWriter(
                    output.with_suffix("").with_name(output.stem + "-traces")
                    / f"{case_id}-{mode}.jsonl"
                )
                agent = Agent(
                    router,
                    ToolExecutor(
                        WorkspacePolicy(root, allow_commands=True, allow_write=True),
                        approval_callback=lambda *a: True,
                    ),
                    on_event=events.append,
                    trace=trace,
                    completion_verifier=ToolEvidenceCompletionVerifier(),
                )
                started = time.perf_counter()
                result = agent.run(task)
                elapsed = time.perf_counter() - started
                results = [
                    json.loads(m["content"]) for m in agent.last_messages if m.get("role") == "tool"
                ]
                routes = [
                    e.data["profile"]
                    for e in events
                    if e.type == "model_routed" and e.data["purpose"] == "execution"
                ]
                matched = bool(routes) and routes[0] == (expected if mode == "auto" else "primary")
                success = any(r["tool_name"] == tool and r["ok"] for r in results)
                writes = sum(r["tool_name"] in {"write_or_replace", "apply_patch"} for r in results)
                row[mode] = {
                    "passed": result.ok
                    and matched
                    and success
                    and writes == 0
                    and not (root / "unexpected.txt").exists(),
                    "initial_profile": routes[0] if routes else None,
                    "strong_execution_requests": routes.count("primary"),
                    "tool_write_executions": writes,
                    "elapsed_seconds": elapsed,
                    "usage": result.usage,
                    "routing": result.routing,
                }
        rows.append(row)
    totals = {}
    for mode in ("auto", "primary"):
        durations = sorted(row[mode]["elapsed_seconds"] for row in rows)
        totals[mode] = {
            "requests": sum(row[mode]["routing"]["requests"] for row in rows),
            "classification_requests": sum(
                row[mode]["routing"]["classification_requests"] for row in rows
            ),
            "strong_execution_requests": sum(
                row[mode]["strong_execution_requests"] for row in rows
            ),
            "total_tokens": sum(row[mode]["usage"]["total_tokens"] for row in rows),
            "escalations": sum(row[mode]["routing"]["escalations"] for row in rows),
            "local_p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1],
        }
    report = {
        "suite": "routing-contract-v2",
        "real_model": False,
        "assumptions": (
            "分类标签由替身预设，分类回复 50 input + 5 output Token，执行回复 100 + 20。"
            "通过率仅表示分流协议与执行边界正确，不是真实模型分类准确率。"
            "不设价格，不报告节省率；新增分类请求也计入总数。延迟仅含本地宿主和工具。"
        ),
        "cases": rows,
        "passed_cases": sum(all(row[m]["passed"] for m in ("auto", "primary")) for row in rows),
        "total_cases": len(rows),
        "totals": totals,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
