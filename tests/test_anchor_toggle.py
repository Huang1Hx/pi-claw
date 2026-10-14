"""M7b 前置：隔离的锚 off/on 开关，只影响锚注入，不影响 pins 与默认行为。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core.schemas import ModelReply
from core.task_anchor import ANCHOR_ENV, resolve_anchor_enabled
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


def make_repl(tmp_path: Path, provider, *, enabled=None) -> CodingRepl:
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    return CodingRepl(
        workspace=workspace,
        provider=provider,
        output=io.StringIO(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        task_anchor_enabled=enabled,
    )


def test_resolve_anchor_enabled_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_anchor_enabled(None, env={}) is True
    assert resolve_anchor_enabled(False, env={ANCHOR_ENV: "on"}) is False
    assert resolve_anchor_enabled(None, env={ANCHOR_ENV: "off"}) is False
    assert resolve_anchor_enabled(None, env={ANCHOR_ENV: "ON"}) is True
    with pytest.raises(ValueError):
        resolve_anchor_enabled(None, env={ANCHOR_ENV: "maybe"})


def test_default_injects_anchor_and_pins(tmp_path: Path) -> None:
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider)
    repl._handle_command("/goal 修复计费回归")
    repl._handle_command("/pin 不要改动评分器")
    repl.run_turn("继续")

    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    assert "修复计费回归" in sent and "不要改动评分器" in sent


def test_off_skips_anchor_but_keeps_pins(tmp_path: Path) -> None:
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider, enabled=False)
    repl._handle_command("/goal 修复计费回归")
    repl._handle_command("/pin 不要改动评分器")
    repl.run_turn("继续")

    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    # 锚临时块被跳过：仅由锚携带的 goal 不再出现；pins 仍单独注入。
    assert "修复计费回归" not in sent
    assert "不要改动评分器" in sent
    # 不静默忽略：显式记录 anchor_disabled。
    events = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event"] == "anchor_disabled" and e["payload"]["run_id"] for e in events)
    # 关闭锚至少不影响 pins/goal 的持久化；锚是否被命令层持久化不影响本回合注入。
    active = repl.load_context()
    assert active.goal == "修复计费回归"
    assert [p.text for p in active.pins] == ["不要改动评分器"]


def test_env_off_disables_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_ENV, "off")
    provider = ProbeProvider([ModelReply.final("done")])
    repl = make_repl(tmp_path, provider)
    assert repl.task_anchor_enabled is False
    repl._handle_command("/goal 修复计费回归")
    repl.run_turn("继续")
    sent = " ".join(str(m.get("content")) for m in provider.seen[0])
    assert "修复计费回归" not in sent
