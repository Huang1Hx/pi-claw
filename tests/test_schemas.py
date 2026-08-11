from __future__ import annotations

import pytest

from pi_claw.schemas import (
    ModelReply,
    PolicyAction,
    PolicyDecision,
    RunResult,
    RunStatus,
    ToolCall,
    ToolResult,
)


@pytest.mark.parametrize("field", ["id", "name"])
@pytest.mark.parametrize("value", ["", None, 0])
def test_tool_call_requires_nonempty_string_fields(field: str, value: object) -> None:
    values = {"id": "call-1", "name": "read_file", "arguments": {}}
    values[field] = value
    with pytest.raises(ValueError):
        ToolCall(**values)  # type: ignore[arg-type]


def test_tool_call_requires_object_arguments() -> None:
    with pytest.raises(TypeError, match="JSON 对象"):
        ToolCall("call-1", "read_file", [])  # type: ignore[arg-type]


def test_tool_call_round_trip() -> None:
    call = ToolCall("call-1", "read_file", {"path": "alpha.txt"})
    assert ToolCall.from_dict(call.to_dict()) == call


@pytest.mark.parametrize(
    ("final_text", "tool_call"),
    [(None, None), ("done", ToolCall("call-1", "read_file", {}))],
)
def test_model_reply_requires_exactly_one_variant(
    final_text: str | None, tool_call: ToolCall | None
) -> None:
    with pytest.raises(ValueError, match="必须且只能"):
        ModelReply(final_text=final_text, tool_call=tool_call)


def test_model_reply_final_text_must_be_string() -> None:
    with pytest.raises(TypeError, match="必须是字符串"):
        ModelReply(final_text=1)  # type: ignore[arg-type]


def test_model_reply_factories_and_serialization() -> None:
    call = ToolCall("call-1", "list_files", {"path": "."})
    assert ModelReply.final("done").to_dict() == {"final_text": "done"}
    assert ModelReply.call(call).to_dict() == {"tool_call": call.to_dict()}


def test_result_and_decision_serialization() -> None:
    result = ToolResult("call-1", "read_file", True, output={"content": "ok"})
    decision = PolicyDecision(PolicyAction.ASK, "需要审批", {"safe": True})
    run = RunResult(RunStatus.COMPLETED, "run-1", 2, final_text="done")

    assert result.to_dict()["ok"] is True
    assert decision.to_dict() == {
        "action": "ask",
        "reason": "需要审批",
        "metadata": {"safe": True},
    }
    assert run.ok is True
    assert run.to_dict()["status"] == "completed"
    assert not RunResult(RunStatus.MAX_STEPS, "run-2", 3).ok
