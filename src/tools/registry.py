"""工具注册与模型 Schema 发现。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ToolHandler = Callable[..., Any]


_TOOL_DESCRIPTIONS = {
    "list_files": "枚举工作区内文件",
    "search_text": "在工作区文本文件中按字面量搜索",
    "read_file": "读取工作区内 UTF-8 文本文件",
    "write_or_replace": "创建新文件，或执行唯一的精确 old 到 new 替换",
    "apply_patch": "对一个文件顺序执行一组唯一、精确的 old 到 new 替换",
    "run_command": "以 argv 数组运行白名单测试或只读 git 命令",
}


class ToolRegistry:
    """工具名到处理函数及模型可见 JSON Schema 的统一注册表。"""

    def __init__(self) -> None:
        """分别保存工具处理函数和模型可见 Schema，两张表使用同一工具名索引。"""
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(
        self, name: str, handler: ToolHandler, schema: dict[str, Any] | None = None
    ) -> None:
        """注册唯一工具名、处理函数和参数 Schema。

        handler 由执行器以 handler(**call.arguments) 调用；返回值会封装为 ToolResult。
        Schema 用来告诉模型参数格式，不替代 Policy 和处理函数内的运行时校验。
        新工具还须扩展 Policy：只注册 handler，不会自动获得执行许可。
        """
        if not name:
            raise ValueError("工具名不能为空")
        if name in self._handlers:
            raise ValueError(f"工具已注册：{name}")
        self._handlers[name] = handler
        self._schemas[name] = schema or {"type": "object", "properties": {}}

    def get(self, name: str) -> ToolHandler | None:
        """查找处理函数，未知名称返回 None。"""
        return self._handlers.get(name)

    def names(self) -> tuple[str, ...]:
        """按注册顺序列出工具名称。"""
        return tuple(self._handlers)

    def openai_schemas(self) -> list[dict[str, Any]]:
        """导出模型可见的 function calling Schema。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": _TOOL_DESCRIPTIONS.get(name, "本地受控工具"),
                    "parameters": self._schemas[name],
                },
            }
            for name in self._handlers
        ]
