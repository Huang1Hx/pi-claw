"""交互输入和斜杠补全；菜单数据来自宿主注册表，不触发模型或执行候选。"""

import os
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.defaults import create_output

from commands.registry import CommandRegistry


class SlashCompleter(Completer):
    """命令和 model 参数补全，普通任务及其他参数不补全，也不读取工作区内容。"""

    def __init__(
        self, registry: CommandRegistry, profiles: Callable[[], Mapping[str, str]]
    ) -> None:
        """按需读取当前模型角色，避免切换配置后菜单保留旧角色。"""
        self.registry, self.profiles = registry, profiles

    def get_completions(self, document: Document, complete_event):
        """从光标前的斜杠前缀生成带中文说明的候选，按 Tab/方向键选择后再回车。"""
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/") or "\n" in text:
            return
        if not any(c.isspace() for c in text):
            for name, command in self.registry.lookup.items():
                if name.startswith(text):
                    yield Completion(
                        name, start_position=-len(text), display_meta=command.description
                    )
            return
        parts = text.split(maxsplit=1)
        if parts[0] != "/model":
            return
        prefix = parts[1] if len(parts) == 2 else ""
        if any(c.isspace() for c in prefix):
            return
        for name, description in self.profiles().items():
            if name.startswith(prefix):
                yield Completion(name, start_position=-len(prefix), display_meta=description)


class TerminalInput:
    """TTY 使用实时菜单；重定向、管道和不支持控制序列的终端退回普通 input。"""

    def __init__(
        self, registry: CommandRegistry, profiles: Callable[[], Mapping[str, str]], output: TextIO
    ):
        """只保存输入配置，不提前创建交互会话或后台读取 stdin。"""
        self.completer = SlashCompleter(registry, profiles)
        self.output = output
        self.session: PromptSession | None = None

    def _bindings(self) -> KeyBindings:
        """单候选 Tab 同步补齐，避免快速 Tab+回车早于后台补全任务而提交半条命令。"""
        bindings = KeyBindings()

        @bindings.add("tab")
        def complete(event) -> None:
            """多候选保持菜单选择，只有明确单候选才直接补到输入框，不执行命令。"""
            buffer = event.current_buffer
            matches = list(
                self.completer.get_completions(
                    buffer.document, CompleteEvent(completion_requested=True)
                )
            )
            if len(matches) == 1:
                buffer.apply_completion(matches[0])
            elif matches:
                if buffer.complete_state:
                    buffer.complete_next()
                else:
                    buffer.start_completion(select_first=True)

        return bindings

    def read(self, prompt: str) -> str:
        """只有真正读取用户任务时启用菜单；审批输入仍由独立审批回调处理。"""
        supported = sys.stdin.isatty() and self.output.isatty() and os.environ.get("TERM") != "dumb"
        if not supported:
            return input(prompt)
        if self.session is None:
            self.session = PromptSession(
                completer=self.completer,
                key_bindings=self._bindings(),
                complete_while_typing=True,
                history=InMemoryHistory(),
                input=create_input(stdin=sys.stdin),
                output=create_output(stdout=self.output),
                reserve_space_for_menu=8,
            )
        return self.session.prompt(prompt)
