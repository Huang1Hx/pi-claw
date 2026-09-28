"""单宿主的多模型顺序工作流；阶段隔离，审查返工有界，工具仍只由 Agent 执行。"""

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from typing import Any

from config.models import WorkflowSettings
from core.completion import ToolEvidenceCompletionVerifier, ToolObservation
from core.schemas import ModelReply, RunResult, ToolCall, ToolResult
from providers import WorkflowStopped
from providers.events import ProviderEvent
from routing.intent import READ_TOOLS
from routing.premium_review import premium_review_reason
from routing.workflow_protocol import MAX_ARTIFACT_CHARS, parse_artifact, phase_messages


class WorkflowCoordinator:
    """规划→编码/验证→常规审查→可选高级审查；工具始终由外层 Agent 执行。"""

    def __init__(self, settings: WorkflowSettings, task: str, request: Callable) -> None:
        """建立本回合阶段状态；request 复用 Router 的全局请求上限、重试和用量入口。"""
        self.settings, self.task, self.request = settings, task, request
        self.phase = "pending"
        self.plan: dict | None = None
        self.review: dict | None = None
        self.premium_review: dict | None = None
        self.premium_review_reason: str | None = None
        self.candidate = ""
        self.feedback: Any = None
        self.phase_history: list[dict] = []
        self.phase_requests = 0
        self.review_rounds = 0
        self.pending: list[ToolCall] = []
        self.observations: list[ToolObservation] = []
        self.changed_files: set[str] = set()
        self.reviewed_files: set[str] = set()
        self.premium_reviewed_files: set[str] = set()
        self.verification: dict | None = None
        self.verifier = ToolEvidenceCompletionVerifier()
        self.terminal_status: str | None = None

    def finish_run(self, result: RunResult) -> None:
        """只有宿主接受最后候选，才将审查批准升级为工作流完成。"""
        self.terminal_status = result.status.value
        if result.ok and self.phase in {"review_approved", "premium_review_approved"}:
            self.phase = "completed"

    def summary(self) -> dict:
        """仅输出产物摘要和真实读取证据，不输出阶段私有推理或 API Key。"""
        return {
            "phase": self.phase,
            "terminal_status": self.terminal_status,
            "review_rounds": self.review_rounds,
            "plan": self.plan,
            "review": self.review,
            "premium_review_triggered": self.premium_review_reason is not None,
            "premium_review_reason": self.premium_review_reason,
            "premium_review": self.premium_review,
            "changed_files": sorted(self.changed_files),
            "reviewed_files": sorted(self.reviewed_files),
            "premium_reviewed_files": sorted(self.premium_reviewed_files),
            "verification": self.verification,
        }

    def observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        """接收宿主真实执行结果；按待执行调用配对，不能用模型声称的结果冒充。"""
        if not self.pending or self.pending[0] != call or result.call_id != call.id:
            raise WorkflowStopped("工作流工具结果与待执行调用不匹配")
        self.pending.pop(0)
        self.phase_history.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": json.dumps(result.to_dict(), ensure_ascii=False),
            }
        )
        self.observations.append(ToolObservation(call, result))
        output = result.output if isinstance(result.output, dict) else {}
        path = output.get("path")
        if result.ok and isinstance(path, str):
            if call.name in self.verifier.WRITE_TOOLS:
                self.changed_files.add(path)
            elif call.name == "read_file" and output.get("truncated") is False:
                if self.phase == "reviewing":
                    self.reviewed_files.add(path)
                elif self.phase == "premium_reviewing":
                    self.premium_reviewed_files.add(path)

    def _start(self, phase: str, history: Sequence[dict]) -> ProviderEvent:
        """切换时新建私有模型对话，保留显式交接产物但不继承上一模型推理状态。"""
        self.phase, self.phase_requests = phase, 0
        if phase == "reviewing":
            self.review_rounds += 1
            self.reviewed_files.clear()
        elif phase == "premium_reviewing":
            self.premium_reviewed_files.clear()
        self.phase_history = phase_messages(
            phase,
            history,
            {
                "task": self.task,
                "plan": self.plan,
                "coding_candidate": self.candidate,
                "feedback": self.feedback,
                "normal_review": self.review,
                "premium_review_reason": self.premium_review_reason,
                "changed_files": sorted(self.changed_files),
                "host_verification": self.verification,
            },
        )
        profile = self._profile()
        return ProviderEvent(
            "workflow_stage",
            {"phase": phase, "profile": profile, "review_round": self.review_rounds},
        )

    def _profile(self) -> str:
        """模型名只能由宿主配置映射，模型 JSON 不能控制此映射。"""
        return {
            "planning": self.settings.planner,
            "coding": self.settings.coder,
            "reviewing": self.settings.reviewer,
            "premium_reviewing": self.settings.premium_reviewer,
        }[self.phase]

    def _artifact(self, kind: str, data: dict) -> ProviderEvent:
        """返回可持久化交接产物；Agent 将其记录为明确标注来源的数据消息。"""
        return ProviderEvent("workflow_artifact", {"phase": self.phase, "kind": kind, "data": data})

    @staticmethod
    def _usage(reply: ModelReply) -> ProviderEvent:
        """中间阶段完整响应虽不交付用户，仍须纳入总 Token 与响应计数。"""
        return ProviderEvent(
            "usage_observed", {"usage": reply.usage.to_dict() if reply.usage else None}
        )

    def _feedback(self, reason: str) -> None:
        """协议或证据缺失时只给固定宿主反馈，不把错误原文提升为系统指令。"""
        self.phase_history.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "host_workflow_feedback", "reason": reason}, ensure_ascii=False
                ),
            }
        )

    def events(
        self, history: Sequence[dict], tools: Sequence[dict], *, stream: bool
    ) -> Iterator[ProviderEvent]:
        """内部消费阶段产物，工具调用交给 Agent；只有审查批准后才交付最终完成候选。"""
        if self.pending:
            raise WorkflowStopped("工作流仍缺少工具结果，拒绝继续请求")
        if self.phase == "pending":
            yield self._start("planning", history)
        elif self.phase == "premium_review_approved":
            raise WorkflowStopped("高级审查已通过但宿主最终验收拒绝，工作流未通过")
        elif self.phase == "review_approved":
            # 外层自定义完成验收仍有最终否决权，不能重复返回已经拒绝的候选。
            if self.review_rounds >= self.settings.max_review_rounds:
                raise WorkflowStopped("宿主拒绝完成且审查轮数已用尽，工作流未通过")
            self.feedback = {"reason": "外层宿主未接受完成，请检查最近反馈"}
            yield self._start("coding", history)
        while True:
            cap = {
                "planning": self.settings.max_planning_requests,
                "reviewing": self.settings.max_review_requests,
                "premium_reviewing": self.settings.max_premium_review_requests,
            }.get(self.phase)
            if cap is not None and self.phase_requests >= cap:
                raise WorkflowStopped(f"{self.phase} 阶段请求上限已达，工作流未通过")
            if (
                len(json.dumps(self.phase_history, ensure_ascii=False))
                > self.settings.max_context_chars
            ):
                raise WorkflowStopped("工作流阶段上下文超过上限，停止而不截断工具协议")
            readonly = self.phase != "coding"
            selected_tools = [
                t for t in tools if not readonly or t["function"]["name"] in READ_TOOLS
            ]
            self.phase_requests += 1
            reply = yield from self.request(
                self._profile(),
                self.phase_history,
                selected_tools,
                purpose=self.phase,
                stream=stream and not readonly,
            )
            # 保存原始 reasoning 仅用于此阶段同一模型续接，不跨阶段传递。
            self.phase_history.append(reply.to_message())
            if reply.tool_calls:
                if readonly and any(call.name not in READ_TOOLS for call in reply.tool_calls):
                    yield self._usage(reply)
                    raise WorkflowStopped(f"{self.phase} 阶段请求非只读工具，已拦截且未执行")
                self.pending = list(reply.tool_calls)
                yield ProviderEvent(
                    "message_complete", reply=replace(reply, tool_call=None, reasoning_content=None)
                )
                return
            if self.phase == "coding":
                yield self._usage(reply)
                if not reply.final_text or len(reply.final_text) > MAX_ARTIFACT_CHARS:
                    raise WorkflowStopped("编码完成说明为空或超过交接上限")
                check = self.verifier.verify(
                    task=self.task, final_text=reply.final_text, observations=self.observations
                )
                self.verification = check.to_dict()
                if not check.accepted:
                    self._feedback(check.reason)
                    yield self._artifact("verification_rejected", self.verification)
                    continue
                self.candidate = reply.final_text
                yield self._artifact(
                    "coding_candidate",
                    {"summary": self.candidate, "verification": self.verification},
                )
                if self.review_rounds >= self.settings.max_review_rounds:
                    raise WorkflowStopped("审查轮数已用尽，工作流未通过")
                yield self._start("reviewing", history)
                continue
            try:
                artifact = parse_artifact(reply, self.phase)
            except (ValueError, TypeError):
                yield self._usage(reply)
                self._feedback("产物 JSON 无效，请严格返回当前阶段要求的字段，勿加 Markdown")
                yield self._artifact("protocol_rejected", {"reason": "产物 JSON 无效"})
                continue
            if self.phase == "planning":
                yield self._usage(reply)
                self.plan = artifact
                yield self._artifact("plan", artifact)
                yield self._start("coding", history)
                continue
            if self.phase == "premium_reviewing":
                self.premium_review = artifact
                if artifact["verdict"] != "approve":
                    yield self._usage(reply)
                    yield self._artifact("premium_review", artifact)
                    raise WorkflowStopped("Kimi 高级审查要求修改，工作流未通过")
                missing = sorted(self.changed_files - self.premium_reviewed_files)
                if missing:
                    yield self._usage(reply)
                    self._feedback("高级审查尚未完整读取这些改动文件：" + ", ".join(missing))
                    yield self._artifact(
                        "premium_review_evidence_rejected", {"unread_files": missing}
                    )
                    continue
                yield self._artifact("premium_review", artifact)
                self.phase = "premium_review_approved"
                yield ProviderEvent(
                    "workflow_stage",
                    {
                        "phase": "premium_review_approved",
                        "profile": self.settings.premium_reviewer,
                        "review_round": self.review_rounds,
                    },
                )
                yield ProviderEvent(
                    "message_complete",
                    reply=replace(
                        reply,
                        final_text=(
                            f"{self.candidate}\n\n常规审查：{self.review['summary']}"
                            f"\n高级审查：{artifact['summary']}"
                        ),
                        reasoning_content=None,
                    ),
                )
                return
            self.review = artifact
            if artifact["verdict"] == "approve":
                missing = sorted(self.changed_files - self.reviewed_files)
                if missing:
                    yield self._usage(reply)
                    self._feedback("审查尚未完整读取这些改动文件：" + ", ".join(missing))
                    yield self._artifact("review_evidence_rejected", {"unread_files": missing})
                    continue
                yield self._artifact("review", artifact)
                reason = premium_review_reason(
                    self.task,
                    self.changed_files,
                    self.review_rounds,
                    self.settings.premium_review_policy,
                )
                if reason is not None:
                    self.premium_review_reason = reason
                    # 常规审查不再作为最终回复交付，必须在切换前单独记录其 usage。
                    yield self._usage(reply)
                    yield self._start("premium_reviewing", history)
                    continue
                self.phase = "review_approved"
                yield ProviderEvent(
                    "workflow_stage",
                    {
                        "phase": "review_approved",
                        "profile": self.settings.reviewer,
                        "review_round": self.review_rounds,
                    },
                )
                # 最后一份审查响应的 usage 由 Agent 正常计入，避免另造零用量的虚拟响应。
                yield ProviderEvent(
                    "message_complete",
                    reply=replace(
                        reply,
                        final_text=f"{self.candidate}\n\n审查结论：{artifact['summary']}",
                        reasoning_content=None,
                    ),
                )
                return
            yield self._usage(reply)
            yield self._artifact("review", artifact)
            if self.review_rounds >= self.settings.max_review_rounds:
                raise WorkflowStopped("审查仍要求修改且轮数已用尽，工作流未通过")
            self.feedback = artifact
            yield self._start("coding", history)
