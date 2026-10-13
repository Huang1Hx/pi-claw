"""Skill 与现有 Agent/后端的端到端兼容性。"""

import io
import json
import os
from pathlib import Path

import pytest

from config.models import SandboxSettings
from core.schemas import ModelReply
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider
from sandbox.docker import DockerCommandRunner
from skills.metrics import lifecycle_completeness
from skills.registry import SkillRegistry

MANIFEST = """\
id = "{sid}"
version = "1.0.0"
description = "示例"
task_types = ["investigation"]
trigger_keywords = ["定位"]
required_tools = ["read_file"]
read_only = true
max_prompt_chars = 500
prompt_file = "prompt.md"
"""


def build_registry(tmp_path: Path) -> SkillRegistry:
    root = tmp_path / "builtin"
    d = root / "repository-investigation"
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.toml").write_text(MANIFEST.format(sid="repository-investigation"), encoding="utf-8")
    (d / "prompt.md").write_text("只读定位步骤。", encoding="utf-8")
    return SkillRegistry.from_builtin(root)


def make_repl(tmp_path, *, sub="only", command_runner=None, trace_name="trace.jsonl"):
    workspace = tmp_path / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    base = tmp_path / sub
    base.mkdir(parents=True, exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=ScriptedProvider([ModelReply.final("done")]),
        output=io.StringIO(),
        store=SessionStore(base / "sessions"),
        trace=TraceWriter(base / trace_name),
        skills_registry=build_registry(tmp_path),
        command_runner=command_runner,
    )


def roles_contents(messages):
    return [(m.get("role"), m.get("content")) for m in messages]


def test_skill_off_matches_default_baseline(tmp_path):
    a = make_repl(tmp_path, sub="a")
    a.run_turn("定位调用链")
    b = make_repl(tmp_path, sub="b")
    b._handle_command("/skill off")
    b.run_turn("定位调用链")
    assert roles_contents(a.messages) == roles_contents(b.messages)


def test_trace_lifecycle_is_complete(tmp_path):
    repl = make_repl(tmp_path, sub="life")
    repl._handle_command("/skill repository-investigation")
    repl.run_turn("定位调用链")
    events = [
        json.loads(line)
        for line in (tmp_path / "life" / "trace.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert lifecycle_completeness(events) == 1.0
    names = [e["event"] for e in events]
    assert "skill_started" in names and "skill_completed" in names
    for event in events:
        if event["event"].startswith("skill_"):
            assert "skill_id" in event["payload"]


def _docker_runner_or_skip():
    runner = DockerCommandRunner(SandboxSettings(backend="docker"), max_output_chars=4096)
    try:
        runner.preflight()
    except (OSError, RuntimeError) as exc:
        if os.environ.get("PI_CLAW_REQUIRE_DOCKER_INTEGRATION") == "1":
            pytest.fail(f"required Docker integration unavailable: {type(exc).__name__}")
        pytest.skip(f"Docker integration unavailable: {type(exc).__name__}")
    return runner


@pytest.mark.docker_integration
def test_docker_backend_selects_and_completes_read_only_skill(tmp_path):
    runner = _docker_runner_or_skip()
    repl = make_repl(tmp_path, sub="docker", command_runner=runner, trace_name="trace-docker.jsonl")
    repl._handle_command("/skill repository-investigation")
    repl.run_turn("定位调用链")
    events = [
        json.loads(line)
        for line in (tmp_path / "docker" / "trace-docker.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert lifecycle_completeness(events) == 1.0
