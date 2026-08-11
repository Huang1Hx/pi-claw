"""受策略约束的本地工具注册表与执行器。"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .policy import WorkspacePolicy
from .schemas import PolicyAction, PolicyDecision, ToolCall, ToolResult
from .workspace import WorkspaceViolation

ToolHandler = Callable[..., Any]
ApprovalCallback = Callable[..., bool]


class ToolRegistry:
    """工具名到处理函数及模型可见 JSON Schema 的统一注册表。"""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(
        self, name: str, handler: ToolHandler, schema: dict[str, Any] | None = None
    ) -> None:
        if not name:
            raise ValueError("工具名不能为空")
        if name in self._handlers:
            raise ValueError(f"工具已注册：{name}")
        self._handlers[name] = handler
        self._schemas[name] = schema or {"type": "object", "properties": {}}

    def get(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def openai_schemas(self) -> list[dict[str, Any]]:
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


_TOOL_DESCRIPTIONS = {
    "list_files": "枚举工作区内文件",
    "search_text": "在工作区文本文件中按字面量搜索",
    "read_file": "读取工作区内 UTF-8 文本文件",
    "write_or_replace": "创建新文件，或执行唯一的精确 old 到 new 替换",
    "apply_patch": "对一个文件顺序执行一组唯一、精确的 old 到 new 替换",
    "run_command": "以 argv 数组运行白名单测试或只读 git 命令",
}

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "runs",
        "tmp",
    }
)
_IGNORED_FILE_NAMES = frozenset({".DS_Store", ".coverage"})


class ToolExecutor:
    """先问 Policy，再决定是否执行注册工具。"""

    def __init__(
        self,
        policy: WorkspacePolicy,
        registry: ToolRegistry | None = None,
        approval_callback: ApprovalCallback | None = None,
        *,
        command_timeout: float = 30.0,
        max_output_chars: int = 100_000,
        max_files: int = 1_000,
        max_matches: int = 1_000,
        max_read_bytes: int = 512_000,
        max_write_bytes: int = 512_000,
        max_scan_file_bytes: int = 1_000_000,
        max_scan_bytes: int = 10_000_000,
    ) -> None:
        if command_timeout <= 0:
            raise ValueError("command_timeout 必须大于 0")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars 必须大于 0")
        limits = (
            max_files,
            max_matches,
            max_read_bytes,
            max_write_bytes,
            max_scan_file_bytes,
            max_scan_bytes,
        )
        if any(limit <= 0 for limit in limits):
            raise ValueError("文件与扫描上限都必须大于 0")
        self.policy = policy
        self.workspace = policy.workspace
        self.approval_callback = approval_callback
        self.command_timeout = command_timeout
        self.max_output_chars = max_output_chars
        self.max_files = max_files
        self.max_matches = max_matches
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes
        self.max_scan_file_bytes = max_scan_file_bytes
        self.max_scan_bytes = max_scan_bytes
        self.registry = registry or ToolRegistry()
        if registry is None:
            self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
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

    def execute(self, call: ToolCall, *, approved: bool = False) -> ToolResult:
        decision = self.policy.evaluate(call)
        policy_metadata = {"policy": decision.to_dict()}
        if decision.action is PolicyAction.DENY:
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"策略拒绝：{decision.reason}",
                metadata=policy_metadata,
            )
        prepared_fingerprint = self._write_fingerprint(call)
        if decision.action is PolicyAction.ASK and not approved:
            approved = self._request_approval(call, decision, self.preview(call))
            if not approved:
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error=f"未获批准：{decision.reason}",
                    metadata=policy_metadata,
                )
            if prepared_fingerprint != self._write_fingerprint(call):
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error="审批后目标文件发生变化，已拒绝执行；请重新预览并审批",
                    metadata=policy_metadata,
                )

        handler = self.registry.get(call.name)
        if handler is None:
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"未知工具：{call.name}",
                metadata=policy_metadata,
            )
        try:
            output = handler(**call.arguments)
            ok = not (
                call.name == "run_command"
                and isinstance(output, dict)
                and output.get("returncode") != 0
            )
            error = None if ok else f"命令退出码为 {output.get('returncode')}"
            return ToolResult(
                call.id,
                call.name,
                ok,
                output=output,
                error=error,
                metadata=policy_metadata,
            )
        except subprocess.TimeoutExpired as exc:
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"命令执行超时（{exc.timeout} 秒）",
                metadata=policy_metadata,
            )
        except Exception as exc:  # 注册表允许扩展工具，任何普通执行错误都应回传给 Agent。
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"工具执行失败：{exc}",
                metadata=policy_metadata,
            )

    def _request_approval(
        self,
        call: ToolCall,
        decision: PolicyDecision,
        preview: str | None,
    ) -> bool:
        callback = self.approval_callback
        if callback is None:
            return False
        try:
            parameters = inspect.signature(callback).parameters
            if len(parameters) >= 3:
                result = callback(call, decision, preview)
            elif len(parameters) >= 2:
                result = callback(call, decision)
            else:
                result = callback(call)
            return result is True
        except (TypeError, ValueError):
            return False

    def preview(self, call: ToolCall, *, max_chars: int = 12_000) -> str | None:
        """为副作用调用生成有界预览；预览失败不会绕过后续真实校验。"""

        if max_chars <= 0:
            raise ValueError("preview max_chars 必须大于 0")
        try:
            if call.name == "run_command":
                argv = call.arguments.get("argv", [])
                cwd = call.arguments.get("cwd", ".")
                return self._limit_preview(
                    f"cwd: {cwd}\nargv: {json.dumps(argv, ensure_ascii=False)}",
                    max_chars,
                )
            if call.name not in {"write_or_replace", "apply_patch"}:
                return None
            path = call.arguments.get("path")
            if not isinstance(path, str):
                return "无法预览：path 不是字符串"
            target = self.workspace.resolve(path, allow_missing=True)
            if target.is_file() and target.stat().st_size > self.max_write_bytes:
                return f"无法预览：目标文件超过上限 {self.max_write_bytes} 字节"
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = before
            if call.name == "write_or_replace":
                old = call.arguments.get("old")
                new = call.arguments.get("new")
                if not isinstance(old, str) or not isinstance(new, str):
                    return "无法预览：old/new 不是字符串"
                if max(len(old.encode("utf-8")), len(new.encode("utf-8"))) > self.max_write_bytes:
                    return f"无法预览：old/new 超过上限 {self.max_write_bytes} 字节"
                if old == "":
                    if target.exists():
                        return "无法预览：创建目标已经存在"
                    after = new
                elif before.count(old) != 1:
                    return f"无法预览：old 出现 {before.count(old)} 次，必须唯一"
                else:
                    after = before.replace(old, new, 1)
            else:
                edits = call.arguments.get("edits")
                if not isinstance(edits, list):
                    return "无法预览：edits 不是数组"
                for index, edit in enumerate(edits, start=1):
                    if not isinstance(edit, dict):
                        return f"无法预览：第 {index} 个 edit 不是对象"
                    old = edit.get("old")
                    new = edit.get("new")
                    if not isinstance(old, str) or not isinstance(new, str) or not old:
                        return f"无法预览：第 {index} 个 edit 无效"
                    if (
                        max(len(old.encode("utf-8")), len(new.encode("utf-8")))
                        > self.max_write_bytes
                    ):
                        return f"无法预览：第 {index} 个 edit 超过字节上限"
                    if after.count(old) != 1:
                        return f"无法预览：第 {index} 个 old 出现 {after.count(old)} 次"
                    after = after.replace(old, new, 1)
            relative = str(target.relative_to(self.workspace.root))
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{relative}" if target.exists() else "/dev/null",
                    tofile=f"b/{relative}",
                )
            )
            preimage = self._write_fingerprint(call)
            preview = f"target-preimage-sha256: {preimage}\n" + (diff or "（文件内容没有变化）")
            return self._limit_preview(preview, max_chars)
        except (OSError, UnicodeDecodeError, WorkspaceViolation, ValueError) as exc:
            return f"无法预览：{exc}"

    @staticmethod
    def _limit_preview(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        marker = "\n...[预览已截断]"
        return value[: max(0, max_chars - len(marker))] + marker

    def _write_fingerprint(self, call: ToolCall) -> str | None:
        if call.name not in {"write_or_replace", "apply_patch"}:
            return None
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return "invalid"
        try:
            target = self.workspace.resolve(path, allow_missing=True)
        except (OSError, WorkspaceViolation):
            return "invalid"
        if not target.exists():
            return "missing"
        if not target.is_file():
            return "not-file"
        try:
            return self._sha256_file(target)
        except OSError:
            return "unreadable"

    @staticmethod
    def _sha256_file(target: Path) -> str:
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(65_536):
                digest.update(block)
        return digest.hexdigest()

    def list_files(self, path: str = ".") -> dict[str, Any]:
        base = self.workspace.resolve(path)
        if base.is_file():
            return {
                "files": [str(base.relative_to(self.workspace.root))],
                "truncated": False,
                "reason": None,
            }
        if not base.is_dir():
            raise ValueError("path 必须是文件或目录")
        found: list[str] = []
        for safe in self._iter_files(base):
            found.append(str(safe.relative_to(self.workspace.root)))
            if len(found) >= self.max_files:
                return {
                    "files": found,
                    "truncated": True,
                    "reason": f"文件数达到上限 {self.max_files}",
                }
        return {"files": found, "truncated": False, "reason": None}

    def search_text(self, query: str, path: str = ".") -> dict[str, Any]:
        if not isinstance(query, str) or not query:
            raise ValueError("query 必须是非空字符串")
        base = self.workspace.resolve(path)
        candidates = [base] if base.is_file() else self._iter_files(base)
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        skipped_large_files = 0
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                safe = self.workspace.resolve(candidate)
                if scanned_files >= self.max_files:
                    return self._search_result(
                        matches,
                        scanned_files,
                        scanned_bytes,
                        skipped_large_files,
                        f"检查文件数达到上限 {self.max_files}",
                    )
                # 大文件即使被跳过也计入检查文件数，避免海量大文件造成无界遍历。
                scanned_files += 1
                size = safe.stat().st_size
                if size > self.max_scan_file_bytes:
                    skipped_large_files += 1
                    continue
                if scanned_bytes + size > self.max_scan_bytes:
                    return self._search_result(
                        matches,
                        scanned_files,
                        scanned_bytes,
                        skipped_large_files,
                        f"总扫描预算达到上限 {self.max_scan_bytes} 字节",
                    )
                text = safe.read_text(encoding="utf-8")
            except (WorkspaceViolation, UnicodeDecodeError, OSError):
                continue
            scanned_bytes += size
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {
                            "path": str(safe.relative_to(self.workspace.root)),
                            "line": line_number,
                            "text": line,
                        }
                    )
                    if len(matches) >= self.max_matches:
                        return self._search_result(
                            matches,
                            scanned_files,
                            scanned_bytes,
                            skipped_large_files,
                            f"匹配数达到上限 {self.max_matches}",
                        )
        return self._search_result(matches, scanned_files, scanned_bytes, skipped_large_files)

    def read_file(self, path: str) -> dict[str, Any]:
        target = self.workspace.resolve(path)
        if not target.is_file():
            raise ValueError("path 必须是已存在的文件")
        with target.open("rb") as stream:
            raw = stream.read(self.max_read_bytes + 1)
        truncated = len(raw) > self.max_read_bytes
        content = raw[: self.max_read_bytes].decode("utf-8", errors="replace")
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "content": content,
            "truncated": truncated,
            "reason": f"读取量达到上限 {self.max_read_bytes} 字节" if truncated else None,
        }

    def write_or_replace(self, path: str, old: str, new: str) -> dict[str, Any]:
        if not all(isinstance(value, str) for value in (path, old, new)):
            raise TypeError("path、old、new 都必须是字符串")
        if len(old.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"old 超过写入处理上限 {self.max_write_bytes} 字节")
        if len(new.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"new 超过写入上限 {self.max_write_bytes} 字节")
        target = self.workspace.resolve(path, allow_missing=True)
        if old == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_create_text(target, new)
            return {"path": str(target.relative_to(self.workspace.root)), "created": True}
        if not target.is_file():
            raise ValueError("精确替换要求目标文件已存在")
        if target.stat().st_size > self.max_write_bytes:
            raise ValueError(f"目标文件超过写入处理上限 {self.max_write_bytes} 字节")
        original_fingerprint = self._sha256_file(target)
        content = target.read_text(encoding="utf-8")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取期间发生变化，拒绝继续")
        count = content.count(old)
        if count == 0:
            raise ValueError("old 文本不存在，未修改文件")
        if count != 1:
            raise ValueError(f"old 文本出现 {count} 次，必须唯一，未修改文件")
        replaced = content.replace(old, new, 1)
        if len(replaced.encode("utf-8")) > self.max_write_bytes:
            raise ValueError(f"替换结果超过写入上限 {self.max_write_bytes} 字节")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取后发生变化，拒绝覆盖")
        self._atomic_replace_text(target, replaced)
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "created": False,
            "replacements": 1,
        }

    def apply_patch(self, path: str, edits: list[dict[str, str]]) -> dict[str, Any]:
        """先在内存中完成全部精确替换，再一次写回目标文件。

        该协议比让模型提交任意 unified diff 更容易校验。任一 ``old`` 不存在或
        不唯一时，整个调用失败且文件保持不变。
        """

        if not isinstance(path, str):
            raise TypeError("path 必须是字符串")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise ValueError("edits 必须包含 1 到 50 个精确替换")
        target = self.workspace.resolve(path)
        if not target.is_file():
            raise ValueError("apply_patch 要求目标文件已存在")
        if target.stat().st_size > self.max_write_bytes:
            raise ValueError(f"目标文件超过写入处理上限 {self.max_write_bytes} 字节")

        original_fingerprint = self._sha256_file(target)
        content = target.read_text(encoding="utf-8")
        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取期间发生变化，拒绝继续")
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
                raise TypeError(f"第 {index} 个 edit 必须且只能包含 old、new")
            old = edit["old"]
            new = edit["new"]
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise ValueError(f"第 {index} 个 edit 的 old 必须为非空字符串")
            count = content.count(old)
            if count != 1:
                raise ValueError(f"第 {index} 个 edit 的 old 出现 {count} 次，必须唯一")
            content = content.replace(old, new, 1)
            if len(content.encode("utf-8")) > self.max_write_bytes:
                raise ValueError(f"补丁结果超过写入上限 {self.max_write_bytes} 字节")

        if self._sha256_file(target) != original_fingerprint:
            raise ValueError("目标文件在读取后发生变化，拒绝覆盖")
        self._atomic_replace_text(target, content)
        return {
            "path": str(target.relative_to(self.workspace.root)),
            "edits_applied": len(edits),
        }

    @staticmethod
    def _write_temporary_text(target: Path, content: str) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise
        return temporary

    @classmethod
    def _atomic_create_text(cls, target: Path, content: str) -> None:
        """用同目录临时文件 + 硬链接实现不覆盖已有目标的原子创建。"""

        temporary = cls._write_temporary_text(target, content)
        try:
            os.link(temporary, target)
            cls._fsync_directory(target.parent)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @classmethod
    def _atomic_replace_text(cls, target: Path, content: str) -> None:
        """完整落盘并 fsync 后再替换，避免进程中断留下半个目标文件。"""

        original_mode = target.stat().st_mode & 0o777
        temporary = cls._write_temporary_text(target, content)
        try:
            temporary.chmod(original_mode)
            os.replace(temporary, target)
            cls._fsync_directory(target.parent)
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def run_command(
        self,
        argv: list[str],
        cwd: str = ".",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise TypeError("argv 必须是非空字符串数组")
        resolved_cwd = self.workspace.resolve(cwd)
        effective_timeout = self.command_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0 or effective_timeout > self.command_timeout:
            raise ValueError(f"timeout 必须在 0 到 {self.command_timeout} 秒之间")
        completed = subprocess.run(
            argv,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
            env=self._restricted_environment(),
        )
        stdout, stdout_truncated = self._truncate(completed.stdout)
        stderr, stderr_truncated = self._truncate(completed.stderr)
        return {
            "argv": argv,
            "cwd": str(resolved_cwd.relative_to(self.workspace.root)) or ".",
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    def _truncate(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_output_chars:
            return value, False
        return value[: self.max_output_chars] + "\n...[输出已截断]", True

    def _iter_files(self, base: Path):
        """逐个产出安全文件，避免 ``sorted(rglob())`` 先把整个仓库载入内存。"""

        for current, directory_names, file_names in os.walk(base, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                if name in _IGNORED_DIRECTORY_NAMES:
                    continue
                try:
                    resolved = self.workspace.resolve(current_path / name)
                except WorkspaceViolation:
                    continue
                if resolved.is_dir():
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                if name in _IGNORED_FILE_NAMES:
                    continue
                candidate = current_path / name
                try:
                    resolved = self.workspace.resolve(candidate)
                except WorkspaceViolation:
                    continue
                if resolved.is_file():
                    yield resolved

    @staticmethod
    def _search_result(
        matches: list[dict[str, Any]],
        scanned_files: int,
        scanned_bytes: int,
        skipped_large_files: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "matches": matches,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "skipped_large_files": skipped_large_files,
            "truncated": reason is not None,
            "reason": reason,
        }

    @staticmethod
    def _restricted_environment() -> dict[str, str]:
        # 仅传递解释器定位、区域设置和临时目录所需字段。采用白名单而非“删除几个
        # 已知字段”，可防止未来新增的 TOKEN/PASSWORD/云凭据意外进入子进程。
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "WINDIR",
            "PATHEXT",
        }
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def create_default_registry(executor: ToolExecutor) -> ToolRegistry:
    """返回执行器已绑定的内置注册表，便于调用方发现工具。"""

    return executor.registry
