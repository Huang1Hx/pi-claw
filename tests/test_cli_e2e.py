from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run_cli(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_MODEL", None)
    env.pop("OPENAI_BASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "interfaces", *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_demo_default_rejects_side_effects_but_completes(tmp_path: Path) -> None:
    trace = tmp_path / "default.jsonl"
    completed = run_cli(tmp_path, "demo", "--trace", str(trace))

    assert completed.returncode == 0, completed.stderr
    assert '"status": "completed"' in completed.stdout
    assert "默认只读" in completed.stdout
    events = read_jsonl(trace)
    rejected = [
        event
        for event in events
        if event["event"] == "tool_result" and not event["payload"]["ok"]  # type: ignore[index]
    ]
    assert len(rejected) == 2
    assert all("未获批准" in event["payload"]["error"] for event in rejected)  # type: ignore[index]


def test_demo_yes_completes_write_and_unittest(tmp_path: Path) -> None:
    trace = tmp_path / "approved.jsonl"
    completed = run_cli(tmp_path, "demo", "--yes", "--trace", str(trace))

    assert completed.returncode == 0, completed.stderr
    assert '"status": "completed"' in completed.stdout
    assert "显式批准" in completed.stdout
    assert "仅对可信工作区" in completed.stderr
    events = read_jsonl(trace)
    tool_results = {
        event["payload"]["tool_name"]: event["payload"]  # type: ignore[index]
        for event in events
        if event["event"] == "tool_result"
    }
    assert tool_results["write_or_replace"]["ok"] is True
    assert tool_results["run_command"]["ok"] is True
    assert tool_results["run_command"]["output"]["returncode"] == 0  # type: ignore[index]


def test_demo_generates_default_trace_in_current_directory(tmp_path: Path) -> None:
    completed = run_cli(tmp_path, "demo")
    traces = list((tmp_path / "runs").glob("pi-claw-*.jsonl"))

    assert completed.returncode == 0, completed.stderr
    assert len(traces) == 1
    assert str(traces[0].resolve()) in completed.stdout
    assert any(event["event"] == "run_completed" for event in read_jsonl(traces[0]))


def test_cli_replay_is_read_only(tmp_path: Path) -> None:
    trace = tmp_path / "source.jsonl"
    generated = run_cli(tmp_path, "demo", "--trace", str(trace))
    assert generated.returncode == 0, generated.stderr
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before_trace = trace.read_bytes()

    replayed = run_cli(tmp_path, "replay", str(trace))

    assert replayed.returncode == 0, replayed.stderr
    assert '"event": "run_started"' in replayed.stdout
    assert '"event": "tool_call"' in replayed.stdout
    assert trace.read_bytes() == before_trace
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_cli_scripted_run_is_offline_and_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    trace = tmp_path / "run.jsonl"

    completed = run_cli(
        tmp_path,
        "run",
        "列出文件",
        "--workspace",
        str(workspace),
        "--provider",
        "scripted",
        "--trace",
        str(trace),
    )

    assert completed.returncode == 0, completed.stderr
    assert '"status": "completed"' in completed.stdout
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "content"
    assert any(event["event"] == "tool_result" for event in read_jsonl(trace))


def test_cli_chat_print_creates_resumable_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "session-data"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo", encoding="utf-8")
    first_trace = tmp_path / "first.jsonl"

    created = run_cli(
        tmp_path,
        "chat",
        "只读查看项目",
        "--print",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
        "--trace",
        str(first_trace),
    )

    assert created.returncode == 0, created.stderr
    assert "Pi-Claw Coding Agent" in created.stdout
    assert "assistant> 离线 Coding Agent 已完成只读检查" in created.stdout
    match = re.search(r"session: ([a-f0-9]{32})", created.stdout)
    assert match is not None
    session_id = match.group(1)
    assert f"--session {session_id}" in created.stdout

    listed = run_cli(
        tmp_path,
        "sessions",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
    )
    assert listed.returncode == 0, listed.stderr
    assert session_id in listed.stdout

    resumed = run_cli(
        tmp_path,
        "chat",
        "继续只读检查",
        "--print",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
        "--session",
        session_id,
        "--trace",
        str(tmp_path / "resumed.jsonl"),
    )
    assert resumed.returncode == 0, resumed.stderr
    assert f"session: {session_id}" in resumed.stdout


def test_cli_chat_continue_uses_latest_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "session-data"
    workspace.mkdir()
    created = run_cli(
        tmp_path,
        "chat",
        "first",
        "--print",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
        "--trace",
        str(tmp_path / "first.jsonl"),
    )
    match = re.search(r"session: ([a-f0-9]{32})", created.stdout)
    assert match is not None

    continued = run_cli(
        tmp_path,
        "chat",
        "second",
        "--print",
        "--continue",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
        "--trace",
        str(tmp_path / "second.jsonl"),
    )
    assert continued.returncode == 0, continued.stderr
    assert f"session: {match.group(1)}" in continued.stdout


def test_cli_continue_reuses_openai_provider_metadata_without_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "session-data"
    workspace.mkdir()
    from persistence.session_store import SessionStore

    session = SessionStore(data_dir).create(
        workspace,
        "system",
        provider="openai",
        model="provider-model",
        base_url="https://example.invalid/v1",
    )

    resumed = run_cli(
        tmp_path,
        "chat",
        "继续",
        "--print",
        "--continue",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(data_dir),
        "--trace",
        str(tmp_path / "resume-openai.jsonl"),
    )

    assert resumed.returncode == 1
    assert f"session: {session.session_id}" in resumed.stdout
    assert "provider: openai" in resumed.stdout
    assert "缺少 OPENAI_API_KEY" in resumed.stdout


def test_cli_chat_print_requires_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    completed = run_cli(
        tmp_path,
        "chat",
        "--print",
        "--workspace",
        str(workspace),
        "--data-dir",
        str(tmp_path / "sessions"),
        "--trace",
        str(tmp_path / "trace.jsonl"),
    )
    assert completed.returncode == 2
    assert "--print 需要提供初始任务" in completed.stdout


def test_cli_eval_writes_report_and_case_traces(tmp_path: Path) -> None:
    report = tmp_path / "eval-report.json"
    traces = tmp_path / "eval-traces"

    completed = run_cli(
        tmp_path,
        "eval",
        "--output",
        str(report),
        "--traces-dir",
        str(traces),
        "--workers",
        "3",
    )

    assert completed.returncode == 0, completed.stderr
    assert "结果：8/8 cases 通过" in completed.stdout
    assert "安全场景通过率：100.0%" in completed.stdout
    assert "workers=3/3" in completed.stdout
    assert "隔离违规=0" in completed.stdout
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["provider"] == "scripted"
    assert value["network_used"] is False
    assert value["metrics"]["assertion_failures"] == 0
    assert value["metrics"]["max_parallel_cases"] >= 2
    assert value["metrics"]["completion_rejections"] == 1
    assert value["metrics"]["unverified_runs"] == 1
    assert len(list(traces.glob("*.jsonl"))) == 8


def test_cli_eval_can_run_one_case_and_rejects_unknown_case(tmp_path: Path) -> None:
    selected = run_cli(
        tmp_path,
        "eval",
        "--case",
        "fix_and_verify",
        "--output",
        str(tmp_path / "selected.json"),
    )
    unknown = run_cli(
        tmp_path,
        "eval",
        "--case",
        "does_not_exist",
        "--output",
        str(tmp_path / "unknown.json"),
    )

    assert selected.returncode == 0, selected.stderr
    assert "结果：1/1 cases 通过" in selected.stdout
    assert "N/A（未选择安全 case）" in selected.stdout
    assert unknown.returncode == 2
    assert "未知评测 case" in unknown.stderr
