"""SkillExecutor：工具子集校验、受限 guidance 生成与生命周期 Trace。"""

import json
from pathlib import Path

import pytest

from persistence.trace import TraceWriter
from security.policy import WorkspacePolicy
from skills.errors import ToolNotAllowed
from skills.executor import SkillExecutor, policy_allowed_tools
from skills.models import SelectionMethod, Skill, SkillManifest, SkillSelection

KNOWN_TOOLS = frozenset(
    {"read_file", "search_text", "list_files", "write_or_replace", "run_command"}
)


def make_skill(
    sid="repository-investigation", *, read_only=True, tools=None, prompt="步骤一。\n步骤二。"
) -> Skill:
    manifest = SkillManifest.from_dict(
        {
            "id": sid,
            "version": "1.2.3",
            "description": "d",
            "task_types": ["investigation"],
            "trigger_keywords": ["定位"],
            "required_tools": tools or ["read_file"],
            "read_only": read_only,
            "max_prompt_chars": 500,
            "prompt_file": "prompt.md",
        },
        known_tools=KNOWN_TOOLS,
    )
    return Skill(manifest=manifest, prompt=prompt, source=Path("/tmp") / sid)


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_prepare_renders_bounded_guidance_and_records_lifecycle(tmp_path):
    trace = TraceWriter(tmp_path / "t.jsonl", run_id="run-1")
    skill = make_skill()
    selection = SkillSelection(skill, SelectionMethod.KEYWORD, "命中定位")
    ex = SkillExecutor()
    prepared = ex.prepare(
        selection, task="定位调用链", allowed_tools=KNOWN_TOOLS, trace=trace, run_id="run-1", step=1
    )
    assert prepared is not None
    assert "repository-investigation" in prepared.guidance
    assert "步骤一" in prepared.guidance
    assert "不能覆盖" in prepared.guidance
    ex.complete(prepared, status="completed", trace=trace, run_id="run-1", step=2)

    events = [e["event"] for e in read_events(tmp_path / "t.jsonl")]
    assert events == ["skill_selected", "skill_started", "skill_completed"]
    completed = read_events(tmp_path / "t.jsonl")[-1]["payload"]
    assert completed["skill_id"] == "repository-investigation"
    assert completed["skill_version"] == "1.2.3"
    assert completed["result_status"] == "completed"
    assert isinstance(completed["duration_ms"], int)


def test_none_selection_returns_none(tmp_path):
    ex = SkillExecutor()
    prepared = ex.prepare(
        SkillSelection(None, SelectionMethod.NONE, "无匹配"),
        task="x",
        allowed_tools=KNOWN_TOOLS,
        trace=TraceWriter(tmp_path / "t.jsonl"),
    )
    assert prepared is None
    assert not (tmp_path / "t.jsonl").exists()


def test_tool_not_allowed_rejected(tmp_path):
    trace = TraceWriter(tmp_path / "t.jsonl", run_id="r")
    skill = make_skill(read_only=False, tools=["read_file", "write_or_replace"])
    selection = SkillSelection(skill, SelectionMethod.EXPLICIT, "显式")
    with pytest.raises(ToolNotAllowed):
        SkillExecutor().prepare(
            selection, task="t", allowed_tools=frozenset({"read_file"}), trace=trace, run_id="r"
        )
    events = [e["event"] for e in read_events(tmp_path / "t.jsonl")]
    assert events == ["skill_selected", "skill_rejected"]
    assert "skill_started" not in events


def test_complete_records_failure(tmp_path):
    trace = TraceWriter(tmp_path / "t.jsonl", run_id="r")
    prepared = SkillExecutor().prepare(
        SkillSelection(make_skill(), SelectionMethod.EXPLICIT, "显式"),
        task="t",
        allowed_tools=KNOWN_TOOLS,
        trace=trace,
        run_id="r",
    )
    SkillExecutor().complete(prepared, status="failed", trace=trace, run_id="r")
    assert read_events(tmp_path / "t.jsonl")[-1]["event"] == "skill_failed"


def test_trace_does_not_contain_full_prompt(tmp_path):
    trace = TraceWriter(tmp_path / "t.jsonl", run_id="r")
    secret_prompt = "UNIQUE-PROMPT-MARKER-1234567890 " + "x" * 100
    ex = SkillExecutor()
    prepared = ex.prepare(
        SkillSelection(make_skill(prompt=secret_prompt), SelectionMethod.EXPLICIT, "显式"),
        task="t",
        allowed_tools=KNOWN_TOOLS,
        trace=trace,
        run_id="r",
    )
    ex.complete(prepared, status="completed", trace=trace, run_id="r")
    assert "UNIQUE-PROMPT-MARKER" not in (tmp_path / "t.jsonl").read_text()


def test_read_only_policy_subset(tmp_path):
    policy = WorkspacePolicy(tmp_path, allow_write=False, allow_commands=False)
    allowed = policy_allowed_tools(policy)
    assert {"read_file", "search_text", "list_files"} <= allowed
    assert "write_or_replace" not in allowed
