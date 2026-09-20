"""交互命令注册、错误恢复、模型切换与数据隔离。"""

import io

import pytest

from commands.registry import Command, CommandRegistry
from config import ModelProfile, Settings
from interfaces.repl import CodingRepl
from persistence.session_store import SessionStore
from persistence.trace import TraceWriter
from providers import OfflineCodingProvider


def repl_for(workspace, tmp_path):
    return CodingRepl(
        workspace=workspace,
        provider=OfflineCodingProvider(),
        store=SessionStore(tmp_path / "sessions"),
        trace=TraceWriter(tmp_path / "trace.jsonl"),
        output=io.StringIO(),
        settings=Settings(
            {
                "economy": ModelProfile("economy", "https://small.invalid", "small", "secret"),
                "primary": ModelProfile("primary", "https://large.invalid", "large", "secret"),
            }
        ),
    )


def test_commands_never_enter_model_context(workspace, tmp_path):
    repl = repl_for(workspace, tmp_path)
    original = list(repl.messages)
    for command in ["/status", "/model", "/sessions", "/help", "/usage", "/unknown"]:
        assert repl._handle_command(command)
    assert repl.messages == original
    assert "secret" not in repl.output.getvalue()
    assert "/status" in repl.output.getvalue() and "/model" in repl.output.getvalue()
    assert not repl._handle_command("/exit")
    assert repl._handle_command("/quit") and "/quit" not in repl.commands.lookup


def test_model_switch_creates_fresh_session_and_persists_selection(workspace, tmp_path):
    repl = repl_for(workspace, tmp_path)
    first_id = repl.session.session_id
    repl.run_turn("only old context")
    repl._handle_command("/model economy")
    assert repl.session.session_id != first_id
    assert len(repl.messages) == 1
    assert repl.session.provider == "routed" and repl.session.model == "economy"
    assert repl.last_result is None
    selected = repl.session.session_id
    repl._handle_command("/model absent")
    assert repl.session.session_id == selected
    assert "未知模型" in repl.output.getvalue()
    repl._handle_command("/model scripted")
    assert repl.provider_name == "scripted"
    repl._handle_command("/resume " + first_id)
    assert repl.session.session_id == first_id


def test_invalid_command_does_not_end_interaction(workspace, tmp_path):
    repl = repl_for(workspace, tmp_path)
    assert repl._handle_command("/resume ../invalid")
    assert repl._handle_command("/title")
    assert "命令错误" in repl.output.getvalue()


def test_registry_rejects_name_and_alias_conflicts_atomically():
    registry = CommandRegistry()
    registry.register(Command("/exit", "/exit", "exit", lambda *a: False, ("/quit",)))
    with pytest.raises(ValueError):
        registry.register(Command("/other", "/other", "other", lambda *a: True, ("/quit",)))
    assert "/other" not in registry.lookup
