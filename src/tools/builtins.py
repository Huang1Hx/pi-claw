"""内置工具的模型可见 Schema。"""

from __future__ import annotations


class BuiltinTools:
    """由 ToolExecutor 绑定工作区和资源预算后使用。"""

    def _register_builtin_tools(self) -> None:
        """把六个协议工具名绑定到当前执行器方法与参数 Schema；注册不等于授予权限。"""
        path_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        }
        self.registry.register("list_files", self.list_files, path_schema)
        self.registry.register(
            "search_text",
            self.search_text,
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        self.registry.register("read_file", self.read_file, path_schema)
        self.registry.register(
            "write_or_replace",
            self.write_or_replace,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        )
        self.registry.register(
            "apply_patch",
            self.apply_patch,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["old", "new"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "edits"],
                "additionalProperties": False,
            },
        )
        self.registry.register(
            "run_command",
            self.run_command,
            {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        )
