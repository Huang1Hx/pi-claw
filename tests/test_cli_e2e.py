from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_MODEL", None)
    env.pop("OPENAI_BASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "pi_claw", *arguments],
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
