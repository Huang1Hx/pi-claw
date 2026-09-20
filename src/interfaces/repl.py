"""Kimi CLI 路线的轻量交互式 Coding Agent 会话。"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from commands import create_commands
from commands.model import current_model, model_options, resolve_model_selection
from config import Settings
from core.agent import Agent
from core.completion import ToolEvidenceCompletionVerifier
from core.context import ContextManager
from core.events import AgentEvent
from core.prompts import build_system_prompt
from core.schemas import RunStatus
from interfaces.rendering import ReplyDisplay
from interfaces.terminal_input import TerminalInput
from persistence.session_store import SessionInfo, SessionStore
from persistence.trace import TraceWriter, redact
from providers import OfflineCodingProvider, Provider
from routing import RoutedProvider
from security.approval import TerminalApprovalBroker
from security.policy import WorkspacePolicy
from tools import ToolExecutor

InputFunction = Callable[[str], str]


class CodingRepl:
    """管理 REPL、持久 Session、审批状态与多轮 Agent 历史。"""

    def __init__(
        self,
        *,
        workspace: str | Path,
        provider: Provider,
        provider_name: str = "scripted",
        model: str | None = None,
        base_url: str | None = None,
        store: SessionStore,
        trace: TraceWriter,
        max_steps: int = 12,
        session_id: str | None = None,
        continue_latest: bool = False,
        auto_approve: bool = False,
        interactive_approvals: bool | None = None,
        input_fn: InputFunction = input,
        output: TextIO | None = None,
        streaming: bool = True,
        settings: Settings | None = None,
        max_attempts: int = 3,
    ) -> None:
        """恢复或创建会话、加载带序号的活动快照并装配运行时；模型在用户回合才调用。"""
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        self.provider = provider
        self.provider_name = provider_name
        self.model = model
        self.base_url = base_url
        self.store = store
        self.trace = trace
        self.max_steps = max_steps
        self.input_fn = input_fn
        self.output = output or sys.stdout
        self.auto_approve = auto_approve
        self.interactive_approvals = interactive_approvals
        self.context = ContextManager()
        self.streaming = streaming
        self.display = ReplyDisplay(self.output)
        self.settings = settings
        self.max_attempts = max_attempts
        self.commands = create_commands()
        self.terminal_input = TerminalInput(
            self.commands, lambda: model_options(self.settings), self.output
        )
        self.last_result = None

        selected: SessionInfo | None = None
        if session_id:
            selected = self.store.load(session_id, self.workspace)
        elif continue_latest:
            selected = self.store.latest(self.workspace)
        self.session = selected or self._create_session()
        if selected is not None and (
            selected.provider != self.provider_name
            or selected.model != self.model
            or selected.base_url != self.base_url
        ):
            self.session = self.store.update_runtime(
                selected.session_id,
                self.workspace,
                provider=self.provider_name,
                model=self.model,
                base_url=self.base_url,
            )
        self.messages, self._session_seq = self.store.load_active_snapshot(
            self.session.session_id,
            self.workspace,
            repair_incomplete=True,
        )
        self.store.save_active_messages(
            self.session.session_id,
            self.workspace,
            self.messages,
            expected_seq=self._session_seq,
        )
        self._refresh_recovery_status()
        self._build_runtime()

    def _create_session(self) -> SessionInfo:
        """根据工作区入口生成系统提示，创建不含密钥的新会话及初始消息。"""
        prompt = build_system_prompt(self.workspace)
        return self.store.create(
            self.workspace,
            prompt,
            provider=self.provider_name,
            model=self.model,
            base_url=self.base_url,
        )

    def _build_runtime(self) -> None:
        """连接审批、Policy、工具、Agent 和存储/界面回调；启用能力仍须通过审批。"""
        self.approval = TerminalApprovalBroker(
            approved_scopes=set(self.session.approved_tools),
            auto_approve=self.auto_approve,
            input_fn=self.input_fn,
            interactive=self.interactive_approvals,
            on_state_change=self._persist_approvals,
        )
        policy = WorkspacePolicy(
            self.workspace,
            allow_write=True,
            allow_commands=True,
            command_profile="coding",
        )
        executor = ToolExecutor(policy, approval_callback=self.approval)
        system_prompt = next(
            (
                str(message.get("content", ""))
                for message in self.messages
                if message.get("role") == "system"
            ),
            build_system_prompt(self.workspace),
        )
        self.agent = Agent(
            self.provider,
            executor,
            trace=self.trace,
            max_steps=self.max_steps,
            system_prompt=system_prompt,
            context_manager=self.context,
            on_message=self._persist_message,
            completion_verifier=ToolEvidenceCompletionVerifier(),
            on_event=self._on_event,
            streaming=self.streaming,
        )

    def _on_event(self, event: AgentEvent) -> None:
        """即时显示候选文本；正式完成与否仍由宿主验收和最终状态决定。"""
        self.display.on_event(event)
        if event.type == "llm_retry":
            print(
                f"retry> 第 {event.data['next_attempt']} 次请求，"
                f"等待 {event.data['delay_seconds']} 秒",
                file=self.output,
            )
        if event.type in {"model_routed", "route_escalated"}:
            print(f"route> {event.data['profile']}：{event.data['reason']}", file=self.output)
        if event.type == "intent_classified":
            print(f"intent> {event.data['complexity']} → {event.data['profile']}", file=self.output)
        if event.type == "workflow_stage":
            labels = {
                "planning": "规划（只读）",
                "coding": "编码与验证",
                "reviewing": "独立审查（只读）",
                "review_approved": "审查通过，等待宿主验收",
            }
            phase = labels.get(event.data["phase"], event.data["phase"])
            print(f"workflow> {phase}：{event.data['profile']}", file=self.output)
        if (
            event.type == "workflow_artifact"
            and event.data.get("kind") == "review"
            and event.data["data"]["verdict"] == "changes_requested"
        ):
            print(
                f"review> {event.data['data']['verdict']}：{event.data['data']['summary']}",
                file=self.output,
            )

    def _persist_message(self, message: dict[str, Any]) -> None:
        """用本地快照序号提交消息并接收新序号；检测并发写入后要求重新加载会话。"""
        # Agent 已把 message 加入同一 self.messages 列表，日志与活动快照在一次提交中推进。
        self._session_seq = self.store.append_message(
            self.session.session_id,
            self.workspace,
            message,
            active_messages=self.messages,
            expected_seq=self._session_seq,
        )
        self.session = self.store.load(self.session.session_id, self.workspace)
        self._print_runtime_message(message)

    def _print_runtime_message(self, message: dict[str, Any]) -> None:
        """显示工具进展，但不输出完整文件内容或未脱敏凭据。"""

        role = message.get("role")
        calls = message.get("tool_calls")
        if role == "assistant" and isinstance(calls, list):
            for raw_call in calls:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function", {})
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name", "unknown"))
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(str(raw_arguments))
                except json.JSONDecodeError:
                    arguments = str(raw_arguments)
                preview = json.dumps(redact(arguments), ensure_ascii=False)
                print(f"tool> {name} {preview[:500]}", file=self.output)
            return
        if role != "tool":
            return
        try:
            result = json.loads(str(message.get("content", "{}")))
        except json.JSONDecodeError:
            print("result> 工具返回无法解析", file=self.output)
            return
        if not isinstance(result, dict):
            print("result> 工具返回不是对象", file=self.output)
            return
        ok = bool(result.get("ok"))
        error = result.get("error")
        output = result.get("output")
        summary = error if error else json.dumps(redact(output), ensure_ascii=False)
        print(f"result> {'ok' if ok else 'failed'} {str(summary)[:500]}", file=self.output)

    def _persist_approvals(self, scopes: set[str]) -> None:
        """保存当前会话的已批准 scope；不修改全局权限或其他会话。"""
        self.session = self.store.update_state(
            self.session.session_id,
            self.workspace,
            approved_tools=sorted(scopes),
        )

    def run(self, initial_prompt: str | None = None, *, print_only: bool = False) -> int:
        """运行交互主循环，返回 CLI 退出码。

        initial_prompt 可先执行一次任务；print_only 为 True 时输出后结束，不进入交互。
        / 命令走命令注册表，普通文本才进入 Agent；EOF、Ctrl+C 或 /exit 退出循环。
        """
        self._print_banner()
        if initial_prompt:
            exit_code = self.run_turn(initial_prompt)
            if print_only:
                self._print_resume_hint()
                return exit_code
        elif print_only:
            print("--print 需要提供初始任务。", file=self.output)
            return 2

        while True:
            try:
                reader = self.terminal_input.read if self.input_fn is input else self.input_fn
                text = reader("pi-claw> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=self.output)
                break
            if not text:
                continue
            # /status、/model 等是宿主命令，不应当作任务发给模型产生 Token 消耗。
            if text.startswith("/"):
                if not self._handle_command(text):
                    break
                continue
            self.run_turn(text)
        self._print_resume_hint()
        return 0

    def run_turn(self, text: str) -> int:
        """运行一个用户回合，持久化活动窗口并显示完成状态。"""
        self.display.reset()
        result = self.agent.run(text, messages=self.messages)
        self.last_result = result
        if result.usage is not None:
            self.store.record_usage(
                self.session.session_id, self.workspace, result.run_id, result.usage
            )
        # Provider 在压缩后直接失败时不会产生新消息；此处仍要保存活动窗口。
        self.store.save_active_messages(
            self.session.session_id,
            self.workspace,
            self.messages,
            expected_seq=self._session_seq,
        )
        self.display.finish(result)
        if result.status is not RunStatus.COMPLETED:
            print(f"运行未完成：{result.error or result.status.value}", file=self.output)
            return 1
        return 0

    def _handle_command(self, raw: str) -> bool:
        """将用户命令交给注册表，REPL 只负责输入输出。"""
        return self.commands.dispatch(raw, self)

    def select_model(self, name: str) -> None:
        """显式切换模型后新建会话，防止旧对话无提示发送到另一供应商。"""
        name = resolve_model_selection(name, self.settings)
        if name == "scripted":
            provider, provider_name, model = OfflineCodingProvider(), "scripted", None
        else:
            if self.settings is None:
                raise ValueError("请通过 --config 加载模型 profile")
            provider = RoutedProvider(self.settings, mode=name, max_attempts=self.max_attempts)
            provider_name, model = "routed", name
        self.provider, self.provider_name, self.model = provider, provider_name, model
        self.base_url = None
        from commands.sessions import new_session

        new_session(self, "")
        self.last_result = None
        print(
            f"已选择：{current_model(self)}，旧会话保留，可通过 /sessions 查看。", file=self.output
        )

    def _print_banner(self) -> None:
        """展示工作区、会话和 Provider；若修复过中断调用，同时提示未重放副作用。"""
        banner = (
            f"Pi-Claw Coding Agent\nworkspace: {self.workspace}\n"
            f"session: {self.session.session_id}\nprovider: {self.provider_name}"
        )
        print(
            banner,
            file=self.output,
        )
        print(
            "命令：输入 / 查看菜单，Tab/↑↓ 选择，回车执行；/help 帮助，/exit 退出。",
            file=self.output,
        )
        if self.recovered_tool_calls:
            print(
                f"恢复提示：{self.recovered_tool_calls} 个中断工具调用已标记为失败，未重放副作用。",
                file=self.output,
            )

    @staticmethod
    def _count_synthetic_recoveries(messages: list[dict[str, Any]]) -> int:
        """统计活动消息中明确带 synthetic_recovery 标记的工具失败结果。"""
        count = 0
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                result = json.loads(str(message.get("content", "{}")))
            except json.JSONDecodeError:
                continue
            metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
            if isinstance(metadata, dict) and metadata.get("synthetic_recovery") is True:
                count += 1
        return count

    def _refresh_recovery_status(self) -> None:
        """更新中断修复计数，有恢复记录时将摘要写入审计轨迹。"""
        self.recovered_tool_calls = self._count_synthetic_recoveries(self.messages)
        if self.recovered_tool_calls:
            self.trace.record(
                "session_recovered_incomplete_tools",
                {
                    "session_id": self.session.session_id,
                    "count": self.recovered_tool_calls,
                },
            )

    def _print_sessions(self) -> None:
        """列出当前工作区的历史会话，并标记正在使用的会话。"""
        sessions = self.store.list_sessions(self.workspace)
        if not sessions:
            print("当前工作区没有会话。", file=self.output)
            return
        for info in sessions:
            marker = "*" if info.session_id == self.session.session_id else " "
            model = f"/{info.model}" if info.model else ""
            print(
                f"{marker} {info.session_id}  {info.updated_at}  "
                f"{info.provider}{model}  {info.title}",
                file=self.output,
            )

    def _print_history(self) -> None:
        """按消息顺序显示单行截断预览；只展示活动窗口，不读取完整历史日志。"""
        for index, message in enumerate(self.messages, start=1):
            role = str(message.get("role", "unknown"))
            content = str(message.get("content", ""))
            preview = " ".join(content.split())[:160]
            print(f"{index:>3} {role:<9} {preview}", file=self.output)

    def _print_resume_hint(self) -> None:
        """生成包含工作区、会话和本机配置路径的恢复命令，不输出密钥内容。"""
        options = []
        if self.settings and self.settings.config_path:
            options.extend(["--config", str(self.settings.config_path)])
        if self.settings and self.settings.env_file and self.settings.env_file.exists():
            options.extend(["--env-file", str(self.settings.env_file)])
        print(
            "恢复此会话："
            + shlex.join(
                [
                    "uv",
                    "run",
                    "--frozen",
                    "pi-claw",
                    "chat",
                    "--workspace",
                    str(self.workspace),
                    "--session",
                    self.session.session_id,
                    *options,
                ]
            ),
            file=self.output,
        )
