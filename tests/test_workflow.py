"""多模型阶段流的离线端到端验证；真实工具、审批、宿主验收，只有模型用替身。"""

import json
import sys
from copy import deepcopy
from dataclasses import replace

import pytest

from config import ModelProfile, RoutingSettings, Settings, WorkflowSettings
from core.agent import Agent
from core.completion import CompletionCheck, ToolEvidenceCompletionVerifier
from core.schemas import ModelReply, RunStatus, ToolCall
from core.usage import TokenUsage
from providers import ProviderError, ScriptedProvider
from routing import RoutedProvider
from routing.intent import READ_TOOLS
from routing.workflow_protocol import parse_artifact
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def answer(text="完成修改并验证", *, thought=None):
    """每个完整响应固定 100 Token，用于检验中间产物无漏记或重复计数。"""
    return ModelReply(final_text=text, usage=TokenUsage(80, 20, 100), reasoning_content=thought)


def plan():
    """合法规划，包含步骤和验证方式。"""
    return answer(
        json.dumps({"summary": "修改文本", "steps": ["改 alpha"], "verification": ["测试"]})
    )


def review(approve=True, summary=None):
    """独立审查结果，否决必须带可执行的问题清单。"""
    return answer(
        json.dumps(
            {
                "verdict": "approve" if approve else "changes_requested",
                "summary": summary or ("符合要求" if approve else "需要修正"),
                "issues": [] if approve else ["alpha.txt 应使用大写 ALPHA"],
            }
        )
    )


def call(name, args, id="call", *, thought=None):
    """工具请求与真实结果通过调用 ID 配对；思考内容仅允许同阶段续接。"""
    return ModelReply(
        tool_calls=(ToolCall(id, name, args),),
        usage=TokenUsage(80, 20, 100),
        reasoning_content=thought,
    )


def read(id="read", **args):
    """读取变更文件，可传小字节上限模拟截断。"""
    return call("read_file", {"path": "alpha.txt", **args}, id)


def read_path(path, id):
    """读取指定改动文件，用于验证多文件高级审查。"""
    return call("read_file", {"path": path}, id)


def write(old="alpha", new="Alpha", id="write"):
    """由真实 ToolExecutor 在临时工作区执行的精确替换。"""
    return call("write_or_replace", {"path": "alpha.txt", "old": old, "new": new}, id)


def create(path, id):
    """通过真实写工具创建新文件。"""
    return call("write_or_replace", {"path": path, "old": "", "new": "content\n"}, id)


def check(id="check"):
    """用当前 uv Python 运行临时工作区中的离线 unittest。"""
    return call("run_command", {"argv": [sys.executable, "-m", "unittest", "test_sample.py"]}, id)


def settings(**workflow):
    """开启工作流，保持所有 profile 的假 Key/URL 一致。"""
    return Settings(
        {
            n: ModelProfile(n, "https://gateway.invalid/v1", n, "fake-key")
            for n in ("economy", "planner", "primary", "reviewer", "premium_reviewer")
        },
        RoutingSettings(intent_strategy="model", max_requests=36),
        workflow=WorkflowSettings(enabled=True, **workflow),
    )


def premium_settings(**workflow):
    """开启风险型高级审查，其余预算沿用工作流默认值。"""
    return settings(
        premium_reviewer="premium_reviewer",
        premium_review_policy="risk",
        **workflow,
    )


def execute(
    workspace,
    scripts,
    *,
    cfg=None,
    mode="auto",
    task="修改 alpha.txt 并验证",
    approve=True,
    verifier=None,
    max_steps=24,
):
    """构造真实 Agent，保存每个角色收到的隔离消息与工具 Schema。"""
    scripts = {"economy": [answer('{"complexity":"complex"}')], **scripts}
    requests, events, journal = [], [], []
    pool = {}
    for name, replies in scripts.items():
        # 异常替身用于验证供应商失败边界；普通列表仍走既有脚本 Provider。
        scripted = replies if isinstance(replies, BaseException) else ScriptedProvider(replies)

        class Recording:
            """绑定各角色的脚本和请求捕获，不使用网络。"""

            def __init__(self, name, scripted):
                """每个 profile 单独保存脚本游标。"""
                self.name, self.scripted = name, scripted

            def complete(self, messages, tools):
                """记录请求快照，避免后续历史追加改变断言证据。"""
                requests.append((self.name, deepcopy(messages), deepcopy(tools)))
                if isinstance(self.scripted, BaseException):
                    raise self.scripted
                return self.scripted.complete(messages, tools)

        pool[name] = Recording(name, scripted)
    router = RoutedProvider(cfg or settings(), mode=mode, factory=lambda p: pool[p.name])
    agent = Agent(
        router,
        ToolExecutor(
            WorkspacePolicy(workspace, allow_write=True, allow_commands=True),
            approval_callback=lambda *args: approve,
        ),
        max_steps=max_steps,
        on_event=events.append,
        on_message=journal.append,
        trace=workspace / "workflow-trace.jsonl",
        completion_verifier=verifier or ToolEvidenceCompletionVerifier(),
    )
    result = agent.run(task)
    return result, agent, requests, events, journal


def test_plan_code_review_real_tools_and_accounting(workspace):
    """完整闭环共 8 个响应、800 mock Token；原始思考不进入跨阶段交接或落盘。"""
    result, agent, requests, events, journal = execute(
        workspace,
        {
            "planner": [
                replace(read("p-read"), tool_call=None, reasoning_content="planner-private"),
                plan(),
            ],
            "primary": [
                replace(write(), tool_call=None, reasoning_content="coder-private"),
                check(),
                answer(),
            ],
            "reviewer": [read("r-read"), review()],
        },
    )
    assert result.ok, result.error
    assert (workspace / "alpha.txt").read_text().startswith("Alpha")
    assert result.routing["requests_by_purpose"] == {
        "classification": 1,
        "planning": 2,
        "coding": 3,
        "reviewing": 2,
    }
    assert result.usage["requests"] == result.routing["requests"] == 8
    assert result.usage["total_tokens"] == 800
    state = result.routing["workflow"]
    assert state["phase"] == state["terminal_status"] == "completed"
    assert state["review_rounds"] == 1 and state["changed_files"] == ["alpha.txt"]
    assert state["reviewed_files"] == ["alpha.txt"] and state["verification"]["accepted"]
    assert [e.data["phase"] for e in events if e.type == "workflow_stage"] == [
        "planning",
        "coding",
        "reviewing",
        "review_approved",
    ]
    for name, messages, tools in requests:
        if name in {"planner", "reviewer"}:
            assert {t["function"]["name"] for t in tools} <= READ_TOOLS
        if name != "planner":
            assert "planner-private" not in repr(messages)
        if name != "primary":
            assert "coder-private" not in repr(messages)
    primary_requests = [m for n, m, _ in requests if n == "primary"]
    assert "coder-private" in repr(primary_requests[1])  # 同模型工具续接必须保留
    for marker in ("planner-private", "coder-private"):
        assert marker not in repr(journal) + repr(events) + repr(agent.last_messages)
        assert marker not in (workspace / "workflow-trace.jsonl").read_text()
    artifacts = [
        json.loads(m["content"])["kind"]
        for m in journal
        if m["role"] == "user" and '"type": "workflow_artifact"' in m["content"]
    ]
    assert artifacts == ["plan", "coding_candidate", "review"]


def test_low_risk_change_does_not_call_premium_reviewer(workspace):
    """普通单文件修改仍停在常规审查，不能因为配置了 Kimi 就产生付费请求。"""
    result, _, requests, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": [read("normal-read"), review(summary="常规审查通过")],
        },
        cfg=premium_settings(),
    )
    assert result.ok, result.error
    assert not any(name == "premium_reviewer" for name, _, _ in requests)
    assert result.routing["workflow"]["premium_review_triggered"] is False


@pytest.mark.parametrize("task", ["修改 alpha.txt 并进行 Kimi 审查", "修改认证和权限并验证"])
def test_explicit_or_high_risk_change_enters_premium_review_once(workspace, task):
    """显式要求和高风险任务都会在常规批准后进入一次独立只读审查。"""
    result, _, requests, events, journal = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": [read("normal-read"), review(summary="常规审查通过")],
            "premium_reviewer": [
                read("premium-read"),
                review(summary="Kimi 高级审查通过"),
            ],
        },
        cfg=premium_settings(),
        task=task,
    )
    assert result.ok, result.error
    assert [name for name, _, _ in requests].count("premium_reviewer") == 2
    assert result.routing["requests_by_purpose"]["premium_reviewing"] == 2
    assert result.usage["total_tokens"] == 900
    state = result.routing["workflow"]
    assert state["phase"] == state["terminal_status"] == "completed"
    assert state["premium_review_triggered"] is True
    assert state["premium_reviewed_files"] == ["alpha.txt"]
    assert state["premium_review"]["summary"] == "Kimi 高级审查通过"
    stages = [event.data["phase"] for event in events if event.type == "workflow_stage"]
    assert stages[-2:] == ["premium_reviewing", "premium_review_approved"]
    premium_tools = [tools for name, _, tools in requests if name == "premium_reviewer"]
    assert all(
        {tool["function"]["name"] for tool in tools} <= READ_TOOLS for tools in premium_tools
    )
    artifacts = [
        json.loads(message["content"])["kind"]
        for message in journal
        if message["role"] == "user" and '"type": "workflow_artifact"' in message["content"]
    ]
    assert artifacts[-2:] == ["review", "premium_review"]


def test_second_normal_review_round_triggers_premium_review(workspace):
    """常规审查返工后才批准时，即使任务和路径普通也升级 Kimi。"""
    result, _, requests, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer(), answer("按常规意见重新确认")],
            "reviewer": [read("r1"), review(False), read("r2"), review()],
            "premium_reviewer": [read("premium"), review()],
        },
        cfg=premium_settings(),
    )
    assert result.ok, result.error
    assert result.routing["workflow"]["premium_review_reason"] == "常规审查经历多轮返工"
    assert [name for name, _, _ in requests].count("premium_reviewer") == 2


def test_five_changed_files_trigger_one_premium_review(workspace):
    """达到文件数量阈值时升级一次，两个审查阶段都必须重新读取五个文件。"""
    paths = [f"part_{index}.txt" for index in range(5)]
    result, _, requests, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [
                *[create(path, f"write-{index}") for index, path in enumerate(paths)],
                check(),
                answer(),
            ],
            "reviewer": [
                *[read_path(path, f"review-{index}") for index, path in enumerate(paths)],
                review(),
            ],
            "premium_reviewer": [
                *[read_path(path, f"premium-{index}") for index, path in enumerate(paths)],
                review(),
            ],
        },
        cfg=premium_settings(max_review_requests=8, max_premium_review_requests=8),
        task="创建五个普通模块并验证",
    )
    assert result.ok, result.error
    state = result.routing["workflow"]
    assert state["premium_review_reason"] == "改动文件数量达到高级审查阈值"
    assert state["premium_reviewed_files"] == paths
    assert [name for name, _, _ in requests].count("premium_reviewer") == 6


@pytest.mark.parametrize("failure", ["reject", "write", "request_cap"])
def test_premium_review_failure_never_completes(workspace, failure):
    """Kimi 否决、越权或耗尽预算都必须保持未验证状态。"""
    premium = {
        "reject": [read("premium"), review(False)],
        "write": [write(id="premium-write")],
        "request_cap": [read("premium")],
    }[failure]
    limits = {"max_premium_review_requests": 1} if failure == "request_cap" else {}
    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": [read("normal"), review()],
            "premium_reviewer": premium,
        },
        cfg=premium_settings(**limits),
        task="修改 alpha.txt 并进行 Kimi 审查",
    )
    assert result.status is RunStatus.UNVERIFIED
    assert result.routing["workflow"]["phase"] == "premium_reviewing"


def test_premium_provider_failure_never_completes(workspace):
    """Kimi 网络或鉴权失败必须保留 provider_error，不能沿用普通审查的批准。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": [read("normal"), review()],
            "premium_reviewer": ProviderError("temporary premium failure"),
        },
        cfg=premium_settings(),
        task="修改 alpha.txt 并进行 Kimi 审查",
    )
    assert result.status is RunStatus.PROVIDER_ERROR
    state = result.routing["workflow"]
    assert state["phase"] == "premium_reviewing"
    assert state["terminal_status"] == "provider_error"


def test_outer_verifier_can_reject_premium_approval(workspace):
    """Kimi approve 仍不能覆盖宿主最终验收。"""

    class Reject:
        def verify(self, **kwargs):
            return CompletionCheck(False, "额外业务验证未满足")

    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": [read("normal"), review()],
            "premium_reviewer": [read("premium"), review()],
        },
        cfg=premium_settings(),
        task="修改 alpha.txt 并进行 Kimi 审查",
        verifier=Reject(),
    )
    assert result.status is RunStatus.UNVERIFIED
    assert result.routing["workflow"]["phase"] == "premium_review_approved"
    assert result.routing["workflow"]["terminal_status"] == "unverified"


def test_reviewer_feedback_drives_repair_and_fresh_verification(workspace):
    """审查否决后重新编码、验证、读取；旧验证不能覆盖新写入。"""
    result, _, requests, events, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [
                write(),
                check(),
                answer(),
                write("Alpha", "ALPHA", "repair"),
                answer("过早宣布完成"),
                check("recheck"),
                answer("返工完成"),
            ],
            "reviewer": [read(), review(False), read("r2"), review()],
        },
    )
    assert result.ok, result.error
    assert (workspace / "alpha.txt").read_text().startswith("ALPHA")
    assert result.routing["workflow"]["review_rounds"] == 2
    assert result.routing["requests"] == 13 and result.usage["total_tokens"] == 1300
    coder = [m for n, m, _ in requests if n == "primary"]
    assert "ALPHA" in coder[3][1]["content"] and "changes_requested" in coder[3][1]["content"]
    assert any(
        e.type == "workflow_artifact" and e.data["kind"] == "verification_rejected" for e in events
    )


@pytest.mark.parametrize("role", ["planner", "reviewer"])
def test_readonly_roles_cannot_write_even_with_approval(workspace, role):
    """宿主拒绝不该出现的工具，不能靠模型自律或用户的通用写入批准。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [write()] if role == "planner" else [plan()],
            "primary": [answer("无需改动")],
            "reviewer": [write()] if role == "reviewer" else [review()],
        },
    )
    assert result.status is RunStatus.UNVERIFIED
    assert "非只读工具" in result.error
    assert (workspace / "alpha.txt").read_text().startswith("alpha")


@pytest.mark.parametrize("reread", [False, True])
def test_approval_requires_full_fresh_read(workspace, reread):
    """未读取或只读取截断片段均不能取得审查通过。"""
    reviewer = ([read(max_bytes=1)] if reread else []) + [review()] * 2
    result, _, _, events, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), check(), answer()],
            "reviewer": reviewer,
        },
        cfg=settings(max_review_requests=len(reviewer)),
    )
    assert result.status is RunStatus.UNVERIFIED and "阶段请求上限" in result.error
    assert result.routing["workflow"]["phase"] == "reviewing"
    assert any(
        e.type == "workflow_artifact" and e.data["kind"] == "review_evidence_rejected"
        for e in events
    )


def test_review_round_limit_stops_without_false_success(workspace):
    """连续否决到达上限后停止，不返回旧的编码成功文本。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [answer(), answer()],
            "reviewer": [review(False), review(False)],
        },
        cfg=settings(max_review_rounds=2),
    )
    assert result.status is RunStatus.UNVERIFIED and result.final_text is None
    assert result.routing["workflow"]["review_rounds"] == 2


def test_unverified_write_never_enters_review(workspace):
    """编码声称完成不能绕过宿主的写后验证；每次拒绝也占总请求预算。"""
    cfg = replace(settings(), routing=RoutingSettings(intent_strategy="model", max_requests=5))
    result, _, requests, events, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), answer(), answer()],
        },
        cfg=cfg,
    )
    assert result.status is RunStatus.PROVIDER_ERROR and "请求上限" in result.error
    assert result.routing["requests"] == result.usage["requests"] == 5
    assert not any(n == "reviewer" for n, _, _ in requests)
    assert (
        sum(
            e.type == "workflow_artifact" and e.data["kind"] == "verification_rejected"
            for e in events
        )
        == 2
    )


def test_write_still_requires_existing_approval(workspace):
    """自动切换角色不等于自动批准写入；拒绝后可如实结束为未修改说明。"""
    result, _, _, events, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [write(), answer("用户拒绝，未修改文件")],
            "reviewer": [review()],
        },
        approve=False,
    )
    assert result.ok  # 没有实际写入的解释性回复不声称验证了修改
    assert result.routing["workflow"]["changed_files"] == []
    assert (workspace / "alpha.txt").read_text().startswith("alpha")
    assert any(e.type == "tool_result" and not e.data["ok"] for e in events)


def test_repeated_read_across_roles_is_not_a_loop(workspace):
    """三种职责各读一次属于必要取证，不应触发三次重复的死循环保护。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [read("p"), plan()],
            "primary": [read("c"), answer()],
            "reviewer": [read("r"), review()],
        },
    )
    assert result.ok, result.error


def test_repetition_inside_one_phase_still_stops(workspace):
    """阶段隔离没有关闭重复检测，同一规划模型的无进展读循环仍被阻止。"""
    result, _, _, _, _ = execute(workspace, {"planner": [read("1"), read("2"), read("3")]})
    assert result.status is RunStatus.LOOP_DETECTED


def test_outer_completion_verifier_has_final_veto(workspace):
    """审查 approve 不覆盖自定义宿主验收，摘要也不能误标 completed。"""

    class Reject:
        """额外业务验收始终拒绝。"""

        def verify(self, **kwargs):
            """模拟项目专用检查未满足。"""
            return CompletionCheck(False, "额外业务验证未满足")

    result, _, _, _, _ = execute(
        workspace,
        {
            "planner": [plan()],
            "primary": [answer()],
            "reviewer": [review()],
        },
        cfg=settings(max_review_rounds=1),
        verifier=Reject(),
    )
    assert result.status is RunStatus.UNVERIFIED
    assert result.routing["workflow"]["phase"] == "review_approved"
    assert result.routing["workflow"]["terminal_status"] == "unverified"


@pytest.mark.parametrize("mode", ["simple", "primary"])
def test_simple_or_fixed_model_bypasses_workflow(workspace, mode):
    """简单问答不增加规划审查开销；显式固定模型仍尊重用户选择。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "economy": [answer('{"complexity":"simple"}'), answer()],
            "primary": [answer()],
        },
        mode="auto" if mode == "simple" else mode,
        task="你好",
    )
    assert result.ok and result.routing["workflow"] is None
    assert result.routing["requests"] == (2 if mode == "simple" else 1)


def test_cheap_unsafe_call_escalates_into_workflow(workspace):
    """轻量执行误发写工具时，丢弃候选后进入规划，不直接带权限重放。"""
    result, _, _, _, _ = execute(
        workspace,
        {
            "economy": [answer('{"complexity":"simple"}'), write()],
            "planner": [plan()],
            "primary": [answer()],
            "reviewer": [review()],
        },
        task="你好",
    )
    assert result.ok and result.routing["escalations"] == 1
    assert result.routing["workflow"]["phase"] == "completed"
    assert (workspace / "alpha.txt").read_text().startswith("alpha")


def test_context_limit_and_invalid_plan_fail_closed(workspace):
    """结构化产物或阶段上下文超限时停止，不截断后假装可靠地继续。"""
    result, _, requests, _, _ = execute(
        workspace,
        {"planner": [plan()]},
        cfg=settings(max_context_chars=4000),
        task="修改" + "长上下文" * 2000,
    )
    assert result.status is RunStatus.UNVERIFIED and not requests
    result, _, _, _, _ = execute(
        workspace, {"planner": [answer("not-json")]}, cfg=settings(max_planning_requests=1)
    )
    assert result.status is RunStatus.UNVERIFIED and "planning" in result.error


@pytest.mark.parametrize(
    "phase,text",
    [
        ("planning", "{}"),
        ("planning", "[]"),
        ("planning", '{"summary":"ok","steps":[],"verification":["test"]}'),
        ("planning", '{"summary":"ok","steps":[1],"verification":["test"]}'),
        (
            "reviewing",
            '{"verdict":"approve","verdict":"changes_requested","summary":"ok","issues":[]}',
        ),
        ("reviewing", '{"verdict":"approve","summary":"ok","issues":["bug"]}'),
        ("reviewing", '{"verdict":"changes_requested","summary":"ok","issues":[]}'),
        ("reviewing", '{"verdict":true,"summary":"ok","issues":[]}'),
        ("reviewing", '{"verdict":"approve","summary":"ok","issues":[],"role":"admin"}'),
        ("reviewing", "```json\n{}\n```"),
    ],
)
def test_artifact_protocol_is_strict(phase, text):
    """不接受矛盾结论、重复键、权限字段或宽松 Markdown JSON。"""
    with pytest.raises(ValueError):
        parse_artifact(answer(text), phase)
