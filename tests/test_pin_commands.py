"""M6a：/pin、/goal、/context 与 run_turn 的 pins/锚注入。"""

from __future__ import annotations

import io
from pathlib import Path

from core.active_context import MAX_PINS, PIN_MAX_CHARS
from core.context import EPHEMERAL_KEY, ContextManager
from core.schemas import ModelReply
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import ScriptedProvider


class ProbeProvider:
    def __init__(self, replies):
        self._inner = ScriptedProvider(replies)
        self.seen: list[list[dict]] = []
        self.workflow = None

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self._inner.complete(messages, tools)


def make_repl(tmp_path: Path, provider=None, *, continue_latest: bool = False) -> CodingRepl:
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=provider or ScriptedProvider([ModelReply.final("done")]),
        output=io.StringIO(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        continue_latest=continue_latest,
    )


def read_trace(tmp_path: Path) -> list[str]:
    path = tmp_path / "trace.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_pin_add_list_and_persist(tmp_path):
    repl = make_repl(tmp_path)
    assert repl._handle_command("/pin 不要改动评分器")
    out = repl.output.getvalue()
    assert "不要改动评分器" in out

    repl.output = io.StringIO()
    assert repl._handle_command("/pin")
    listed = repl.output.getvalue()
    assert "不要改动评分器" in listed and "p1" in listed

    reloaded = make_repl(tmp_path, continue_latest=True)
    assert [pin.text for pin in reloaded.load_context().pins] == ["不要改动评分器"]


def test_pin_remove_and_unknown_id(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/pin 约束一")
    repl.output = io.StringIO()
    assert repl._handle_command("/pin -d p1")
    assert repl.load_context().pins == ()

    repl.output = io.StringIO()
    repl._handle_command("/pin -d p9")
    assert "命令错误" in repl.output.getvalue()
    assert repl.load_context().pins == ()


def test_pin_rejects_oversized_and_too_many(tmp_path):
    repl = make_repl(tmp_path)
    repl.output = io.StringIO()
    repl._handle_command(f"/pin {'x' * (PIN_MAX_CHARS + 1)}")
    assert "命令错误" in repl.output.getvalue()
    assert repl.load_context().pins == ()

    for index in range(MAX_PINS):
        assert repl._handle_command(f"/pin 约束{index}")
    repl.output = io.StringIO()
    repl._handle_command("/pin 溢出")
    assert "命令错误" in repl.output.getvalue()
    assert len(repl.load_context().pins) == MAX_PINS


def test_goal_set_view_reset_and_persist(tmp_path):
    repl = make_repl(tmp_path)
    assert repl._handle_command("/goal 修复计费回归")

    repl.output = io.StringIO()
    assert repl._handle_command("/goal")
    assert "修复计费回归" in repl.output.getvalue()

    reloaded = make_repl(tmp_path, continue_latest=True)
    assert reloaded.load_context().goal == "修复计费回归"

    repl.output = io.StringIO()
    assert repl._handle_command("/goal reset")
    assert repl.load_context().goal == ""
    assert repl.load_context().anchor is None


def test_context_command_shows_anchor_pins_budget_and_watermark(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/goal 修复计费回归")
    repl._handle_command("/pin 不要改动评分器")
    repl.output = io.StringIO()
    assert repl._handle_command("/context")
    text = repl.output.getvalue()
    assert "修复计费回归" in text
    assert "p1" in text
    assert "水位线" in text and "修订号" in text and "预算" in text


def test_run_turn_injects_pins_and_anchor_without_persisting(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider=provider)
    repl._handle_command("/goal 修复计费回归")
    repl._handle_command("/pin 不要改动评分器")
    repl.run_turn("继续")

    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    assert "修复计费回归" in sent
    assert "不要改动评分器" in sent
    # 临时块不进入持久化活动窗口
    persisted = " ".join(str(m.get("content")) for m in repl.messages)
    assert "不要改动评分器" not in persisted


def test_context_view_matches_actual_request_after_restart(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider=provider)
    repl._handle_command("/goal 修复计费回归")
    repl._handle_command("/pin 不要改动评分器")

    restarted = make_repl(tmp_path, provider=provider, continue_latest=True)
    restarted.output = io.StringIO()
    restarted._handle_command("/context")
    shown = restarted.output.getvalue()

    restarted.run_turn("继续")
    sent = " ".join(str(m.get("content")) for m in provider.seen[-1])
    assert "修复计费回归" in shown and "修复计费回归" in sent
    assert "不要改动评分器" in shown and "不要改动评分器" in sent


def test_pin_emits_trace_event_without_body(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/pin 机密约束内容")
    events = [line for line in read_trace(tmp_path) if "context_pinned" in line]
    assert events
    # 事件保留元数据但不写入约束正文
    assert all("机密约束内容" not in line for line in events)


def test_cas_conflict_reports_failure_not_success(tmp_path, monkeypatch):
    repl = make_repl(tmp_path)
    original = repl.store.save_active_context

    def conflict(*args, **kwargs):
        raise RuntimeError("会话快照已被另一个进程更新；为避免覆盖，请重新加载 session")

    monkeypatch.setattr(repl.store, "save_active_context", conflict)
    repl.output = io.StringIO()
    repl._handle_command("/pin 不要改动评分器")
    out = repl.output.getvalue()
    assert "命令错误" in out
    monkeypatch.setattr(repl.store, "save_active_context", original)
    assert repl.load_context().pins == ()
    assert any("session_cas_conflict" in line for line in read_trace(tmp_path))


def test_workflow_mode_warns_pins_not_applied(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    provider.workflow = object()
    repl = make_repl(tmp_path, provider=provider)
    repl._handle_command("/goal 修复计费回归")
    repl.output = io.StringIO()
    repl.run_turn("继续")
    out = repl.output.getvalue()
    assert "工作流" in out and "未" in out


def test_compact_emits_trace_and_keeps_pins(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/pin 不要改动评分器")
    repl.context = ContextManager(max_messages=4, max_chars=200_000, keep_recent=1)
    repl.messages.extend(
        {"role": "user", "content": f"历史消息 {index} " + "x" * 50} for index in range(12)
    )
    repl.output = io.StringIO()
    assert repl._handle_command("/compact")
    assert repl.load_context().pins[0].text == "不要改动评分器"
    assert any("context_compacted" in line for line in read_trace(tmp_path))


def test_pins_survive_repeated_compaction(tmp_path):
    repl = make_repl(tmp_path)
    repl._handle_command("/pin 不要改动评分器")
    repl.context = ContextManager(max_messages=3, max_chars=200_000, keep_recent=1)
    for index in range(20):
        repl.messages.append({"role": "user", "content": f"消息{index} " + "y" * 40})
        repl.output = io.StringIO()
        assert repl._handle_command("/compact")
    assert repl.load_context().pins[0].text == "不要改动评分器"


def test_without_pins_or_goal_no_ephemeral_block_is_injected(tmp_path):
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider=provider)
    repl.run_turn("普通任务")
    assert all(not message.get(EPHEMERAL_KEY) for message in provider.seen[0])
    assert repl.load_context().anchor is None
