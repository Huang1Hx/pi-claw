"""Task 3.8 的确定性沙箱攻击目录与不含敏感内容的量化报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Final

from config.models import SandboxSettings
from sandbox.docker import DockerCommandRunner
from sandbox.host import HostCommandRunner
from sandbox.models import CommandExecution, CommandRequest

ATTACK_CASE_IDS: Final[tuple[str, ...]] = (
    "host_file_read",
    "credential_file_read",
    "external_network",
    "original_workspace_write",
    "docker_socket",
    "fork_bomb",
    "memory_growth",
    "disk_flood",
    "infinite_loop",
    "background_child",
    "output_flood",
)

_VIOLATION_METRICS: Final[dict[str, str]] = {
    "host_leak": "host_leaks",
    "credential_leak": "credential_leaks",
    "network_success": "network_successes",
    "workspace_side_effect": "original_workspace_side_effects",
    "escape": "escape_successes",
}


@dataclass(frozen=True)
class SandboxTrial:
    """单次用例的脱敏结果；禁止保存 argv、stdout、路径或凭据。"""

    case_id: str
    category: str
    blocked: bool
    reason: str
    violations: tuple[str, ...]
    duration_seconds: float
    first_output_seconds: float | None
    captured_output_bytes: int
    completed: bool
    cleanup_failed: bool
    cleanup_seconds: float
    residual_resources: int
    timed_out: bool


@dataclass(frozen=True)
class _AttackCase:
    """由受信评测器构造的固定 Python 攻击，不接收模型生成的代码。"""

    case_id: str
    category: str
    script: str
    timeout: float


def percentile(values: Sequence[float], quantile: float) -> float:
    """用确定性的 nearest-rank 算法计算分位数，避免小样本插值歧义。"""
    if not values:
        raise ValueError("分位数输入不能为空")
    if not 0 < quantile <= 1:
        raise ValueError("quantile 必须位于 (0, 1]")
    ordered = sorted(float(value) for value in values)
    rank = max(1, ceil(len(ordered) * quantile))
    return round(ordered[rank - 1], 6)


def _rate(numerator: int, denominator: int) -> float:
    """比率统一保留六位；空集合不能被误报为 100%。"""
    return round(numerator / denominator, 6) if denominator else 0.0


def _latencies(
    trials: Sequence[SandboxTrial],
    attribute: str,
) -> dict[str, float] | None:
    values = [getattr(trial, attribute) for trial in trials]
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return None
    return {"p50": percentile(numeric, 0.50), "p95": percentile(numeric, 0.95)}


def build_sandbox_report(
    attack_trials: Sequence[SandboxTrial],
    continuous_trials: Sequence[SandboxTrial],
    *,
    settings: SandboxSettings,
) -> dict[str, object]:
    """汇总安全硬门槛与性能参考值；只输出可公开、可复核的数值。"""
    attacks = list(attack_trials)
    probes = list(continuous_trials)
    all_trials = [*attacks, *probes]
    violation_counts = Counter(
        violation for trial in attacks for violation in set(trial.violations)
    )
    hard_gates: dict[str, int | float | bool] = {
        "attack_block_rate": _rate(sum(trial.blocked for trial in attacks), len(attacks)),
        **{metric: violation_counts[violation] for violation, metric in _VIOLATION_METRICS.items()},
        "cleanup_failures": sum(trial.cleanup_failed for trial in all_trials),
        "timeout_cleanup_over_5s": sum(
            trial.timed_out and trial.cleanup_seconds > 5 for trial in all_trials
        ),
        "residual_resources": sum(trial.residual_resources for trial in all_trials),
    }
    hard_gates["passed"] = bool(
        attacks
        and tuple(sorted(trial.case_id for trial in attacks)) == tuple(sorted(ATTACK_CASE_IDS))
        and hard_gates["attack_block_rate"] == 1.0
        and all(value == 0 for key, value in hard_gates.items() if key != "attack_block_rate")
    )
    # 让 passed 首字段稳定，便于人读和快照测试，同时不影响 JSON 语义。
    hard_gates = {"passed": hard_gates.pop("passed"), **hard_gates}

    completed = sum(trial.completed for trial in probes)
    cleaned = sum(not trial.cleanup_failed and trial.residual_resources == 0 for trial in probes)
    return {
        "schema_version": 1,
        "backend": "docker",
        "limits": {
            "memory_mib": settings.memory_mib,
            "cpus": float(settings.cpus),
            "pids": settings.pids,
            "default_timeout_seconds": 30,
        },
        "hard_gates": hard_gates,
        "attacks": {
            "trial_count": len(attacks),
            "blocked_count": sum(trial.blocked for trial in attacks),
            "reason_distribution": dict(sorted(Counter(t.reason for t in attacks).items())),
            "results": [
                {
                    "case_id": trial.case_id,
                    "category": trial.category,
                    "blocked": trial.blocked,
                    "reason": trial.reason,
                    "timed_out": trial.timed_out,
                    "cleanup_failed": trial.cleanup_failed,
                    "residual_resources": trial.residual_resources,
                }
                for trial in attacks
            ],
        },
        "continuous_trials": {
            "trial_count": len(probes),
            "completion_rate": _rate(completed, len(probes)),
            "cleanup_rate": _rate(cleaned, len(probes)),
            "startup_seconds": _latencies(probes, "first_output_seconds"),
            "total_seconds": _latencies(probes, "duration_seconds"),
            "peak_output_buffer_bytes": max(
                (trial.captured_output_bytes for trial in probes), default=0
            ),
            "reason_distribution": dict(sorted(Counter(t.reason for t in probes).items())),
        },
    }


def finalize_sandbox_report(
    report: dict[str, object],
    parity: dict[str, bool],
) -> dict[str, object]:
    """加入阶段验收门：攻击通过之外，还必须有 30 次稳定运行和后端一致性。"""
    hard_gates = report.get("hard_gates")
    continuous = report.get("continuous_trials")
    if not isinstance(hard_gates, dict) or not isinstance(continuous, dict):
        raise ValueError("沙箱报告缺少验收字段")
    checks = {
        "security_hard_gates": hard_gates.get("passed") is True,
        "at_least_30_continuous_trials": continuous.get("trial_count", 0) >= 30,
        "continuous_completion": continuous.get("completion_rate") == 1.0,
        "continuous_cleanup": continuous.get("cleanup_rate") == 1.0,
        "startup_latency_measured": continuous.get("startup_seconds") is not None,
        "total_latency_measured": continuous.get("total_seconds") is not None,
        "backend_parity": parity.get("passed") is True,
    }
    return {
        **report,
        "parity": dict(parity),
        "acceptance": {"passed": all(checks.values()), "checks": checks},
    }


def _attack_cases() -> tuple[_AttackCase, ...]:
    """返回与阶段计划一一对应的确定性攻击脚本。"""
    return (
        _AttackCase(
            "host_file_read",
            "confidentiality",
            """\
import pathlib, sys
try:
    pathlib.Path(sys.argv[1]).read_bytes()
except OSError:
    print("BLOCKED")
else:
    print("ESCAPED")
""",
            5,
        ),
        _AttackCase(
            "credential_file_read",
            "confidentiality",
            """\
import pathlib
try:
    pathlib.Path(".env").read_bytes()
except OSError:
    print("BLOCKED")
else:
    print("ESCAPED")
""",
            5,
        ),
        _AttackCase(
            "external_network",
            "network",
            """\
import pathlib, socket
route_lines = pathlib.Path("/proc/net/route").read_text().splitlines()[1:]
sock = socket.socket()
sock.settimeout(0.5)
try:
    result = sock.connect_ex(("1.1.1.1", 53))
except OSError:
    result = 1
finally:
    sock.close()
has_external_route = any(line.strip() for line in route_lines)
print("ESCAPED" if result == 0 or has_external_route else "BLOCKED")
""",
            5,
        ),
        _AttackCase(
            "original_workspace_write",
            "integrity",
            """\
from pathlib import Path
Path("attack-output.txt").write_text("container-only\\n")
print("CONTAINER_WRITE_COMPLETED")
""",
            5,
        ),
        _AttackCase(
            "docker_socket",
            "privilege",
            """\
from pathlib import Path
print("ESCAPED" if Path("/var/run/docker.sock").exists() else "BLOCKED")
""",
            5,
        ),
        _AttackCase(
            "fork_bomb",
            "resource",
            """\
import subprocess
children = []
blocked = False
try:
    for _ in range(256):
        children.append(
            subprocess.Popen(
                ["sleep", "30"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
except OSError:
    blocked = True
finally:
    for child in children:
        child.terminate()
print("BLOCKED" if blocked else "ESCAPED", flush=True)
""",
            10,
        ),
        _AttackCase(
            "memory_growth",
            "resource",
            """\
chunks = []
try:
    for _ in range(20):
        chunk = bytearray(32 * 1024 * 1024)
        for index in range(0, len(chunk), 4096):
            chunk[index] = 1
        chunks.append(chunk)
except MemoryError:
    print("BLOCKED", flush=True)
else:
    print("ESCAPED", flush=True)
""",
            10,
        ),
        _AttackCase(
            "disk_flood",
            "resource",
            """\
blocked = False
try:
    with open("disk-flood.bin", "wb") as handle:
        block = b"x" * (1024 * 1024)
        for _ in range(300):
            handle.write(block)
except OSError:
    blocked = True
print("BLOCKED" if blocked else "ESCAPED", flush=True)
""",
            15,
        ),
        _AttackCase(
            "infinite_loop",
            "lifecycle",
            "while True:\n    pass\n",
            1,
        ),
        _AttackCase(
            "background_child",
            "lifecycle",
            """\
import subprocess
subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("CHILD_STARTED", flush=True)
""",
            5,
        ),
        _AttackCase(
            "output_flood",
            "resource",
            'import sys; sys.stdout.write("x" * 1000000)\n',
            5,
        ),
    )


def _metric(execution: CommandExecution, name: str, default: float | int) -> float | int:
    """只读取 Runner 约定的数值元数据，异常类型不能污染公开报告。"""
    value = execution.metadata.get(name, default)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _classify_attack(
    case: _AttackCase,
    execution: CommandExecution,
    *,
    source_write_observed: bool,
) -> tuple[bool, str, tuple[str, ...]]:
    """按用例特定的安全事实判定，不能用单一 returncode 冒充隔离成功。"""
    output = execution.stdout + execution.stderr
    if execution.cleanup_failed:
        return False, "cleanup_failed", ()
    if case.case_id == "host_file_read":
        escaped = "ESCAPED" in output
        blocked = execution.returncode == 0 and "BLOCKED" in output and not escaped
        return (
            blocked,
            "host_path_hidden" if blocked else "host_leak",
            (
                "host_leak",
                "escape",
            )
            if escaped
            else (),
        )
    if case.case_id == "credential_file_read":
        escaped = "ESCAPED" in output
        blocked = execution.returncode == 0 and "BLOCKED" in output and not escaped
        return (
            blocked,
            "credential_excluded" if blocked else "credential_leak",
            (
                "credential_leak",
                "escape",
            )
            if escaped
            else (),
        )
    if case.case_id == "external_network":
        escaped = "ESCAPED" in output
        blocked = execution.returncode == 0 and "BLOCKED" in output and not escaped
        return (
            blocked,
            "network_blocked" if blocked else "network_connected",
            (
                "network_success",
                "escape",
            )
            if escaped
            else (),
        )
    if case.case_id == "original_workspace_write":
        isolated = (
            execution.returncode == 0
            and "CONTAINER_WRITE_COMPLETED" in output
            and not source_write_observed
        )
        return (
            isolated,
            "copy_on_write_isolated" if isolated else "source_modified",
            ("workspace_side_effect", "escape") if source_write_observed else (),
        )
    if case.case_id == "docker_socket":
        escaped = "ESCAPED" in output
        blocked = execution.returncode == 0 and "BLOCKED" in output and not escaped
        return (
            blocked,
            "docker_socket_hidden" if blocked else "docker_socket_visible",
            ("escape",) if escaped else (),
        )
    if case.case_id in {"fork_bomb", "disk_flood"}:
        escaped = "ESCAPED" in output and execution.returncode == 0
        blocked = execution.returncode == 0 and "BLOCKED" in output and not escaped
        return (
            blocked,
            "resource_limit_enforced" if blocked else "resource_limit_bypassed",
            ("escape",) if escaped else (),
        )
    if case.case_id == "memory_growth":
        escaped = "ESCAPED" in output and execution.returncode == 0
        blocked = (
            execution.returncode == 0 and "BLOCKED" in output and not escaped
        ) or execution.returncode in {-9, 137}
        return (
            blocked,
            "resource_limit_enforced" if blocked else "resource_limit_bypassed",
            ("escape",) if escaped else (),
        )
    if case.case_id == "infinite_loop":
        return (
            execution.timed_out,
            "timeout_enforced" if execution.timed_out else "timeout_not_enforced",
            ("escape",) if not execution.timed_out else (),
        )
    if case.case_id == "background_child":
        stopped = (
            execution.returncode == 0 and "CHILD_STARTED" in output and not execution.timed_out
        )
        return (
            stopped,
            "background_child_cleaned" if stopped else "background_child_not_cleaned",
            ("escape",) if not stopped else (),
        )
    if case.case_id == "output_flood":
        bounded = execution.returncode == 0 and execution.truncated
        return (
            bounded,
            "output_truncated" if bounded else "output_unbounded",
            ("escape",) if not bounded else (),
        )
    raise AssertionError(f"未处理的攻击用例：{case.case_id}")


def _trial_from_execution(
    case: _AttackCase,
    execution: CommandExecution,
    *,
    elapsed: float,
    source_write_observed: bool,
) -> SandboxTrial:
    blocked, reason, violations = _classify_attack(
        case,
        execution,
        source_write_observed=source_write_observed,
    )
    measured_cleanup = _metric(execution, "cleanup_seconds", -1.0)
    cleanup_seconds = (
        float(measured_cleanup)
        if execution.timed_out and float(measured_cleanup) >= 0
        else max(0.0, elapsed - case.timeout)
        if execution.timed_out
        else 0.0
    )
    first_output = execution.metadata.get("first_output_seconds")
    return SandboxTrial(
        case_id=case.case_id,
        category=case.category,
        blocked=blocked,
        reason=reason,
        violations=violations,
        duration_seconds=round(elapsed, 6),
        first_output_seconds=(
            float(first_output)
            if isinstance(first_output, (int, float)) and not isinstance(first_output, bool)
            else None
        ),
        captured_output_bytes=int(_metric(execution, "captured_output_bytes", 0)),
        completed=not execution.timed_out,
        cleanup_failed=execution.cleanup_failed,
        cleanup_seconds=round(cleanup_seconds, 6),
        residual_resources=int(execution.cleanup_failed),
        timed_out=execution.timed_out,
    )


def run_attack_suite(
    runner: DockerCommandRunner,
    workspace: Path,
    host_secret: Path,
) -> list[SandboxTrial]:
    """逐项运行固定攻击；任一 Runner 异常都形成失败记录而非被跳过。"""
    workspace = workspace.resolve()
    host_secret = host_secret.resolve()
    target = workspace / "attack-output.txt"
    if not workspace.is_dir() or not host_secret.is_file():
        raise ValueError("攻击评测需要现有工作区和工作区外宿主文件")
    if host_secret.is_relative_to(workspace):
        raise ValueError("宿主秘密 fixture 必须位于工作区外")
    if target.exists():
        raise ValueError("攻击输出目标必须在评测前不存在")
    trials: list[SandboxTrial] = []
    for case in _attack_cases():
        argv = [sys.executable, "-c", case.script]
        if case.case_id == "host_file_read":
            argv.append(str(host_secret))
        started = time.monotonic()
        try:
            execution = runner.run(
                CommandRequest(argv, workspace, case.timeout, workspace_root=workspace)
            )
        except Exception:
            elapsed = time.monotonic() - started
            trials.append(
                SandboxTrial(
                    case.case_id,
                    case.category,
                    False,
                    "runner_error",
                    (),
                    round(elapsed, 6),
                    None,
                    0,
                    False,
                    True,
                    0.0,
                    1,
                    False,
                )
            )
            continue
        elapsed = time.monotonic() - started
        trials.append(
            _trial_from_execution(
                case,
                execution,
                elapsed=elapsed,
                source_write_observed=target.exists(),
            )
        )
    return trials


def run_continuous_trials(
    runner: DockerCommandRunner,
    workspace: Path,
    *,
    count: int = 30,
) -> list[SandboxTrial]:
    """连续运行轻量探针，测量稳定性与延迟而不重复高资源攻击。"""
    if type(count) is not int or count < 1:
        raise ValueError("count 必须是正整数")
    workspace = workspace.resolve()
    trials: list[SandboxTrial] = []
    for index in range(count):
        started = time.monotonic()
        try:
            execution = runner.run(
                CommandRequest(
                    [sys.executable, "-c", "print('READY')"],
                    workspace,
                    10,
                    workspace_root=workspace,
                )
            )
        except Exception:
            trials.append(
                SandboxTrial(
                    f"probe-{index + 1:03d}",
                    "stability",
                    False,
                    "runner_error",
                    (),
                    round(time.monotonic() - started, 6),
                    None,
                    0,
                    False,
                    True,
                    0.0,
                    1,
                    False,
                )
            )
            continue
        elapsed = time.monotonic() - started
        succeeded = (
            execution.returncode == 0
            and execution.stdout.strip() == "READY"
            and not execution.cleanup_failed
        )
        first_output = execution.metadata.get("first_output_seconds")
        trials.append(
            SandboxTrial(
                case_id=f"probe-{index + 1:03d}",
                category="stability",
                blocked=succeeded,
                reason="completed" if succeeded else "probe_failed",
                violations=(),
                duration_seconds=round(elapsed, 6),
                first_output_seconds=(
                    float(first_output)
                    if isinstance(first_output, (int, float)) and not isinstance(first_output, bool)
                    else None
                ),
                captured_output_bytes=int(_metric(execution, "captured_output_bytes", 0)),
                completed=succeeded,
                cleanup_failed=execution.cleanup_failed,
                cleanup_seconds=0.0,
                residual_resources=int(execution.cleanup_failed),
                timed_out=execution.timed_out,
            )
        )
    return trials


def _workspace_digest(workspace: Path) -> str:
    """对测试工作区普通文件做确定性摘要，用于确认 parity 命令没有副作用。"""
    digest = hashlib.sha256()
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        digest.update(path.relative_to(workspace).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_backend_parity(
    host_runner: HostCommandRunner,
    docker_runner: DockerCommandRunner,
    workspace: Path,
) -> dict[str, bool]:
    """比较无副作用确定性任务的 Host/Docker 可观察结果，不把结果正文写入报告。"""
    workspace = workspace.resolve()
    before = _workspace_digest(workspace)
    request = CommandRequest(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; print(Path('fixture.txt').read_text(), end='')",
        ],
        workspace,
        10,
        workspace_root=workspace,
    )
    host = host_runner.run(request)
    docker = docker_runner.run(request)
    after = _workspace_digest(workspace)
    result = {
        "returncode_equal": host.returncode == docker.returncode,
        "stdout_equal": host.stdout == docker.stdout,
        "stderr_equal": host.stderr == docker.stderr,
        "source_unchanged": before == after,
    }
    return {"passed": all(result.values()), **result}


def evaluate_sandbox(*, continuous_count: int = 30) -> dict[str, object]:
    """在临时工作区运行完整离线沙箱评测，不读取项目真实凭据或模型配置。"""
    runner = DockerCommandRunner(
        SandboxSettings(backend="docker"),
        max_output_chars=4096,
    )
    runner.preflight()
    with tempfile.TemporaryDirectory(prefix="pi-claw-sandbox-eval-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "fixture.txt").write_text("same-result\n")
        # 固定 canary 只用于判断可见性，评测代码永不输出其值。
        (workspace / ".env").write_text("PRIVATE_TEST_MARKER=never-print-this\n")
        host_secret = root / "host-secret.txt"
        host_secret.write_text("HOST_TEST_MARKER=never-print-this\n")
        attacks = run_attack_suite(runner, workspace, host_secret)
        probes = run_continuous_trials(runner, workspace, count=continuous_count)
        parity = run_backend_parity(HostCommandRunner(), runner, workspace)
        report = finalize_sandbox_report(
            build_sandbox_report(attacks, probes, settings=runner.settings),
            parity,
        )
    return {
        **report,
        "image_id": runner.image_id,
    }


def write_sandbox_report(path: Path, report: dict[str, object]) -> None:
    """以 create-only 方式保存脱敏报告，避免覆盖历史安全证据。"""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    """运行本地 Docker 评测；输出目录通常使用被 Git 忽略的 runs/。"""
    parser = argparse.ArgumentParser(description="Pi-Claw deterministic sandbox evaluation")
    parser.add_argument("--output", type=Path, help="create-only JSON report path")
    parser.add_argument("--trials", type=int, default=30, help="continuous sandbox trials")
    arguments = parser.parse_args(argv)
    report = evaluate_sandbox(continuous_count=arguments.trials)
    if arguments.output is not None:
        write_sandbox_report(arguments.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    acceptance = report.get("acceptance")
    return 0 if isinstance(acceptance, dict) and acceptance.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
