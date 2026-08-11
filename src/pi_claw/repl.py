"""Kimi CLI 路线的轻量交互式 Coding Agent 会话。"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .agent import Agent
from .approval import TerminalApprovalBroker
from .context import ContextManager
from .policy import WorkspacePolicy
from .prompts import build_system_prompt
from .providers import Provider
from .schemas import RunStatus
from .session import SessionInfo, SessionStore
from .tools import ToolExecutor
from .trace import TraceWriter, redact

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
    ) -> None:
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
        prompt = build_system_prompt(self.workspace)
        return self.store.create(
            self.workspace,
            prompt,
            provider=self.provider_name,
            model=self.model,
            base_url=self.base_url,
        )

    def _build_runtime(self) -> None:
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
        )

    def _persist_message(self, message: dict[str, Any]) -> None:
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
        self.session = self.store.update_state(
            self.session.session_id,
            self.workspace,
            approved_tools=sorted(scopes),
        )

    def run(self, initial_prompt: str | None = None, *, print_only: bool = False) -> int:
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
                text = self.input_fn("pi-claw> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=self.output)
                break
            if not text:
                continue
            if text.startswith("/"):
                if not self._handle_command(text):
                    break
                continue
            self.run_turn(text)
        self._print_resume_hint()
        return 0

    def run_turn(self, text: str) -> int:
        result = self.agent.run(text, messages=self.messages)
        # Provider 在压缩后直接失败时不会产生新消息；此处仍要保存活动窗口。
        self.store.save_active_messages(
            self.session.session_id,
            self.workspace,
            self.messages,
            expected_seq=self._session_seq,
        )
        if result.final_text:
            print(f"\nassistant> {result.final_text}\n", file=self.output)
        if result.status is not RunStatus.COMPLETED:
            print(f"运行未完成：{result.error or result.status.value}", file=self.output)
            return 1
        return 0

    def _handle_command(self, raw: str) -> bool:
        command, _, argument = raw.partition(" ")
        argument = argument.strip()
        if command in {"/exit", "/quit"}:
            return False
        if command == "/help":
            print(
                "/help  /exit  /new  /sessions  /resume <id>  /compact [关注点]  "
                "/context  /history  /title <标题>",
                file=self.output,
            )
            return True
        if command == "/new":
            self.session = self._create_session()
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
            print(f"已创建新会话：{self.session.session_id}", file=self.output)
            return True
        if command in {"/sessions", "/resume"} and not argument:
            self._print_sessions()
            return True
        if command == "/resume":
            candidate = self.store.load(argument, self.workspace)
            if (
                candidate.provider != self.provider_name
                or candidate.model != self.model
                or candidate.base_url != self.base_url
            ):
                command_parts = [
                    "uv run pi-claw chat",
                    f"--workspace {shlex.quote(str(self.workspace))}",
                    f"--session {shlex.quote(candidate.session_id)}",
                    f"--provider {shlex.quote(candidate.provider)}",
                ]
                if candidate.model:
                    command_parts.append(f"--model {shlex.quote(candidate.model)}")
                if candidate.base_url:
                    command_parts.append(f"--base-url {shlex.quote(candidate.base_url)}")
                print(
                    "目标会话使用不同的 Provider 运行时。为避免静默混用模型，"
                    "请退出后运行：\n" + " ".join(command_parts),
                    file=self.output,
                )
                return True
            self.session = candidate
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
            print(f"已恢复会话：{self.session.session_id}", file=self.output)
            return True
        if command == "/compact":
            before = len(self.messages)
            self.messages[:] = self.context.compact(self.messages, argument or None)
            self.store.save_active_messages(
                self.session.session_id,
                self.workspace,
                self.messages,
                expected_seq=self._session_seq,
            )
            print(f"上下文已压缩：{before} -> {len(self.messages)} 条活动消息", file=self.output)
            return True
        if command == "/context":
            print(
                f"session={self.session.session_id} messages={len(self.messages)} "
                f"chars={self.context.estimate_chars(self.messages)} "
                f"tokens≈{self.context.estimate_tokens(self.messages)}",
                file=self.output,
            )
            return True
        if command == "/history":
            self._print_history()
            return True
        if command == "/title" and argument:
            self.session = self.store.update_state(
                self.session.session_id,
                self.workspace,
                title=argument,
            )
            print(f"会话标题已更新：{self.session.title}", file=self.output)
            return True
        print(f"未知命令：{raw}。输入 /help 查看命令。", file=self.output)
        return True

    def _print_banner(self) -> None:
        banner = (
            f"Pi-Claw Coding Agent\nworkspace: {self.workspace}\n"
            f"session: {self.session.session_id}\nprovider: {self.provider_name}"
        )
        print(
            banner,
            file=self.output,
        )
        if self.recovered_tool_calls:
            print(
                f"恢复提示：{self.recovered_tool_calls} 个中断工具调用已标记为失败，未重放副作用。",
                file=self.output,
            )

    @staticmethod
    def _count_synthetic_recoveries(messages: list[dict[str, Any]]) -> int:
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
        for index, message in enumerate(self.messages, start=1):
            role = str(message.get("role", "unknown"))
            content = str(message.get("content", ""))
            preview = " ".join(content.split())[:160]
            print(f"{index:>3} {role:<9} {preview}", file=self.output)

    def _print_resume_hint(self) -> None:
        print(
            f"恢复此会话：uv run pi-claw chat --workspace {self.workspace} "
            f"--session {self.session.session_id}",
            file=self.output,
        )
