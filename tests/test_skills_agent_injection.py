"""Agent 对 Skill 指导的单回合注入与结束剥离。"""

import json

from core.agent import Agent
from core.schemas import ModelReply
from security.policy import WorkspacePolicy
from tools import ToolExecutor


class ProbeProvider:
    """记录每次收到的消息，再委托给 ScriptedProvider。"""

    def __init__(self, replies):
        from providers import ScriptedProvider

        self._inner = ScriptedProvider(replies)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self._inner.complete(messages, tools)


GUIDE = "SKILL-GUIDANCE-MARKER 只读定位，不写文件。"


def test_guidance_injected_during_run_and_stripped_after(workspace):
    provider = ProbeProvider([ModelReply.final("done")])
    agent = Agent(
        provider,
        ToolExecutor(WorkspacePolicy(workspace)),
        system_prompt="BASE-SYSTEM-PROMPT",
    )
    messages: list[dict] = []
    result = agent.run("定位问题", messages=messages, skill_guidance=GUIDE)
    assert result.final_text == "done"

    # 运行中模型确实收到了该指导（作为受限 system 消息）
    first = provider.seen[0]
    assert any(m.get("role") == "system" and GUIDE in str(m.get("content")) for m in first)
    # 基础 system prompt 仍然在最前，指导在其后，用户任务在最后
    assert first[0]["content"] == "BASE-SYSTEM-PROMPT"
    assert first[-1]["role"] == "user" and first[-1]["content"] == "定位问题"

    # 运行结束后指导被剥离，不进入持久化历史
    assert all(GUIDE not in json.dumps(m, ensure_ascii=False) for m in messages)
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]


def test_no_guidance_keeps_messages_unchanged(workspace):
    provider = ProbeProvider([ModelReply.final("done")])
    agent = Agent(provider, ToolExecutor(WorkspacePolicy(workspace)), system_prompt="SYS")
    messages: list[dict] = []
    agent.run("任务", messages=messages)
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]


def test_invalid_guidance_type_rejected(workspace):
    agent = Agent(
        ProbeProvider([ModelReply.final("done")]),
        ToolExecutor(WorkspacePolicy(workspace)),
    )
    try:
        agent.run("任务", skill_guidance=123)  # type: ignore[arg-type]
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("非字符串 guidance 应被拒绝")
