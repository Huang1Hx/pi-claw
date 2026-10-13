"""终端接口：/skills、/skill、/status 与 run_turn 注入。"""

import io
from pathlib import Path

import pytest

from core.schemas import ModelReply
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider
from skills.registry import SkillRegistry

MANIFEST = """\
id = "{sid}"
version = "1.0.0"
description = "{desc}"
task_types = ["investigation"]
trigger_keywords = ["定位"]
required_tools = ["read_file"]
read_only = true
max_prompt_chars = 500
prompt_file = "prompt.md"
"""


def build_registry(tmp_path: Path) -> SkillRegistry:
    root = tmp_path / "builtin"
    for sid, desc in [("repository-investigation", "只读定位"), ("python-test-repair", "修复测试")]:
        d = root / sid
        d.mkdir(parents=True)
        (d / "skill.toml").write_text(MANIFEST.format(sid=sid, desc=desc), encoding="utf-8")
        (d / "prompt.md").write_text(f"{sid} 的执行步骤。", encoding="utf-8")
    return SkillRegistry.from_builtin(root)


class ProbeProvider:
    def __init__(self, replies):
        self._inner = ScriptedProvider(replies)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self._inner.complete(messages, tools)


def make_repl(tmp_path, provider=None):
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=provider or ScriptedProvider([ModelReply.final("done")]),
        output=io.StringIO(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        skills_registry=build_registry(tmp_path),
    )


def test_skills_lists_builtin_with_permissions(tmp_path):
    repl = make_repl(tmp_path)
    assert repl._handle_command("/skills")
    text = repl.output.getvalue()
    assert "repository-investigation" in text and "python-test-repair" in text
    assert "只读" in text


def test_skill_auto_and_off(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/skill auto")
    assert repl.skill_mode == "auto"
    repl._handle_command("/skill off")
    assert repl.skill_mode == "off"


def test_skill_explicit_valid_and_invalid(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/skill repository-investigation")
    assert repl.skill_mode == "explicit" and repl.skill_id == "repository-investigation"
    repl._handle_command("/skill does-not-exist")
    assert "命令错误" in repl.output.getvalue()
    assert repl.skill_id == "repository-investigation"


@pytest.mark.parametrize("bad", ["../evil", "/etc/passwd", "a/b", ".."])
def test_skill_rejects_paths(tmp_path, bad):
    repl = make_repl(tmp_path)
    repl._handle_command(f"/skill {bad}")
    assert repl.skill_mode == "off"


def test_status_shows_skill_selection(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/skill repository-investigation")
    repl.output = io.StringIO()
    repl._handle_command("/status")
    assert "repository-investigation" in repl.output.getvalue()


def test_run_turn_injects_selected_skill_guidance(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider=provider)
    repl._handle_command("/skill repository-investigation")
    repl.run_turn("定位调用链")
    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    assert "repository-investigation" in sent
    # 注入不会持久化到活动窗口
    assert all("执行步骤" not in str(m.get("content")) for m in repl.messages)


def test_run_turn_with_skill_off_sends_no_guidance(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider=provider)
    repl.run_turn("定位调用链")
    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    assert "受限执行指导" not in sent
