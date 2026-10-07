"""Task 3.8 的 Docker 攻击评测：单元契约始终运行，真实 Docker 用例可标记跳过。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from config.models import SandboxSettings
from evaluation.sandbox_cases import (
    ATTACK_CASE_IDS,
    SandboxTrial,
    build_sandbox_report,
    finalize_sandbox_report,
    percentile,
    run_attack_suite,
    run_backend_parity,
    run_continuous_trials,
    write_sandbox_report,
)
from sandbox.docker import DockerCommandRunner
from sandbox.host import HostCommandRunner
from sandbox.models import CommandExecution


def _trial(
    case_id: str,
    *,
    blocked: bool = True,
    violations: tuple[str, ...] = (),
    duration: float = 1.0,
    first_output: float | None = 0.1,
    output_bytes: int = 10,
    completed: bool = True,
    cleanup_failed: bool = False,
    cleanup_seconds: float = 0.1,
    residual_resources: int = 0,
    reason: str = "blocked",
) -> SandboxTrial:
    return SandboxTrial(
        case_id=case_id,
        category="test",
        blocked=blocked,
        reason=reason,
        violations=violations,
        duration_seconds=duration,
        first_output_seconds=first_output,
        captured_output_bytes=output_bytes,
        completed=completed,
        cleanup_failed=cleanup_failed,
        cleanup_seconds=cleanup_seconds,
        residual_resources=residual_resources,
        timed_out=False,
    )


def test_attack_catalog_covers_all_stage3_threats() -> None:
    """计划列出的 11 种攻击都必须拥有稳定身份，防止报告悄悄漏项。"""
    assert ATTACK_CASE_IDS == (
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


def test_percentile_uses_deterministic_nearest_rank() -> None:
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.50) == 2.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0


def test_report_exposes_quantitative_hard_gates_without_raw_output_or_paths(
    tmp_path: Path,
) -> None:
    """公开报告只含聚合指标/稳定身份，不保存 stdout、命令和宿主绝对路径。"""
    attacks = [_trial(case_id) for case_id in ATTACK_CASE_IDS]
    probes = [
        _trial(
            f"probe-{index}",
            duration=float(index + 1),
            first_output=float(index + 1) / 10,
            output_bytes=index + 1,
            reason="completed",
        )
        for index in range(30)
    ]

    report = build_sandbox_report(
        attacks,
        probes,
        settings=SandboxSettings(backend="docker"),
    )

    assert report["hard_gates"] == {
        "passed": True,
        "attack_block_rate": 1.0,
        "host_leaks": 0,
        "credential_leaks": 0,
        "network_successes": 0,
        "original_workspace_side_effects": 0,
        "escape_successes": 0,
        "cleanup_failures": 0,
        "timeout_cleanup_over_5s": 0,
        "residual_resources": 0,
    }
    assert report["continuous_trials"]["trial_count"] == 30
    assert report["continuous_trials"]["completion_rate"] == 1.0
    assert report["continuous_trials"]["cleanup_rate"] == 1.0
    assert report["continuous_trials"]["startup_seconds"] == {"p50": 1.5, "p95": 2.9}
    assert report["continuous_trials"]["total_seconds"] == {"p50": 15.0, "p95": 29.0}
    assert report["continuous_trials"]["peak_output_buffer_bytes"] == 30
    assert report["limits"] == {
        "memory_mib": 512,
        "cpus": 1.0,
        "pids": 128,
        "default_timeout_seconds": 30,
    }
    serialized = repr(report)
    assert "stdout" not in serialized
    assert "argv" not in serialized
    assert str(tmp_path) not in serialized


def test_report_fails_closed_for_any_violation_or_cleanup_failure() -> None:
    attacks = [_trial(case_id) for case_id in ATTACK_CASE_IDS]
    attacks[0] = _trial(
        "host_file_read",
        blocked=False,
        violations=("host_leak", "escape"),
    )
    attacks[1] = _trial("credential_file_read", cleanup_failed=True, residual_resources=1)

    report = build_sandbox_report(
        attacks,
        [_trial("probe-0")],
        settings=SandboxSettings(backend="docker"),
    )

    assert report["hard_gates"]["passed"] is False
    assert report["hard_gates"]["host_leaks"] == 1
    assert report["hard_gates"]["escape_successes"] == 1
    assert report["hard_gates"]["cleanup_failures"] == 1
    assert report["hard_gates"]["residual_resources"] == 1


def test_final_acceptance_requires_30_trials_and_backend_parity() -> None:
    attacks = [_trial(case_id) for case_id in ATTACK_CASE_IDS]
    probes = [_trial(f"probe-{index}", reason="completed") for index in range(30)]
    report = build_sandbox_report(
        attacks,
        probes,
        settings=SandboxSettings(backend="docker"),
    )

    accepted = finalize_sandbox_report(report, {"passed": True})
    too_few = finalize_sandbox_report(
        build_sandbox_report(
            attacks,
            probes[:29],
            settings=SandboxSettings(backend="docker"),
        ),
        {"passed": True},
    )
    parity_failed = finalize_sandbox_report(report, {"passed": False})

    assert accepted["acceptance"]["passed"] is True
    assert too_few["acceptance"]["passed"] is False
    assert parity_failed["acceptance"]["passed"] is False


def test_report_rejects_duplicate_attack_identities() -> None:
    """11 条记录不能用重复身份凑齐，必须恰好覆盖威胁目录。"""
    attacks = [_trial(case_id) for case_id in ATTACK_CASE_IDS]
    attacks[-1] = _trial(ATTACK_CASE_IDS[0])

    report = build_sandbox_report(
        attacks,
        [],
        settings=SandboxSettings(backend="docker"),
    )

    assert report["hard_gates"]["passed"] is False


def test_attack_suite_does_not_mistake_script_failure_for_a_block(tmp_path: Path) -> None:
    """空输出/普通失败码不是安全证据，评测脚本坏掉时必须整体 fail closed。"""

    class FailedRunner:
        def run(self, request):
            return CommandExecution(2, "", "script failed", False, backend="docker")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("marker\n")
    host_secret = tmp_path / "secret.txt"
    host_secret.write_text("marker\n")

    trials = run_attack_suite(FailedRunner(), workspace, host_secret)  # type: ignore[arg-type]

    assert all(trial.blocked is False for trial in trials)


def test_report_writer_is_create_only_and_serializes_no_private_fields(tmp_path: Path) -> None:
    """评测报告不可覆盖旧证据，并且只能写已脱敏的聚合对象。"""
    output = tmp_path / "sandbox-report.json"
    report = {"hard_gates": {"passed": True}, "parity": {"passed": True}}

    write_sandbox_report(output, report)

    assert json.loads(output.read_text()) == report
    with pytest.raises(FileExistsError):
        write_sandbox_report(output, report)


def _docker_runner_or_skip() -> DockerCommandRunner:
    runner = DockerCommandRunner(
        SandboxSettings(backend="docker"),
        max_output_chars=4096,
    )
    try:
        runner.preflight()
    except (OSError, RuntimeError) as exc:
        if os.environ.get("PI_CLAW_REQUIRE_DOCKER_INTEGRATION") == "1":
            pytest.fail(f"required Docker integration unavailable: {type(exc).__name__}")
        pytest.skip(f"Docker integration unavailable: {type(exc).__name__}")
    return runner


@pytest.mark.docker_integration
def test_real_docker_blocks_all_attack_cases_and_cleans_resources(tmp_path: Path) -> None:
    """11 类攻击必须全部受限，且秘密文件和原工作区都不能进入容器副作用面。"""
    runner = _docker_runner_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe\n")
    (workspace / ".env").write_text("PRIVATE_TEST_MARKER=never-print-this\n")
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("HOST_TEST_MARKER=never-print-this\n")

    trials = run_attack_suite(runner, workspace, host_secret)
    report = build_sandbox_report(
        trials,
        [],
        settings=runner.settings,
    )

    assert tuple(trial.case_id for trial in trials) == ATTACK_CASE_IDS
    assert report["hard_gates"]["passed"] is True
    assert not (workspace / "attack-output.txt").exists()
    assert (workspace / ".env").read_text() == "PRIVATE_TEST_MARKER=never-print-this\n"
    timeout_trial = next(trial for trial in trials if trial.case_id == "infinite_loop")
    assert timeout_trial.timed_out is True
    assert timeout_trial.cleanup_seconds <= 5


@pytest.mark.docker_integration
def test_real_docker_runs_30_clean_trials_and_matches_host_result(tmp_path: Path) -> None:
    """连续试验采集 P50/P95；确定性只读任务在 Host/Docker 结果必须相同。"""
    runner = _docker_runner_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("same-result\n")

    probes = run_continuous_trials(runner, workspace, count=30)
    report = build_sandbox_report([], probes, settings=runner.settings)
    parity = run_backend_parity(HostCommandRunner(), runner, workspace)

    assert len(probes) == 30
    assert report["continuous_trials"]["completion_rate"] == 1.0
    assert report["continuous_trials"]["cleanup_rate"] == 1.0
    assert report["continuous_trials"]["startup_seconds"]["p95"] >= 0
    assert report["continuous_trials"]["total_seconds"]["p95"] >= 0
    assert parity == {
        "passed": True,
        "returncode_equal": True,
        "stdout_equal": True,
        "stderr_equal": True,
        "source_unchanged": True,
    }
