"""工具调用前置权限策略。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from core.schemas import PolicyAction, PolicyDecision, ToolCall
from security.workspace import Workspace, WorkspaceViolation


class WorkspacePolicy:
    """默认只读策略。

    ``allow_write``/``allow_commands`` 表示调用方允许进入审批流程，不代表免审批；
    安全写入和安全命令始终返回 ASK，必须由 ToolExecutor 获得显式批准。
    """

    READ_TOOLS = frozenset({"list_files", "search_text", "read_file"})
    WRITE_TOOLS = frozenset({"write_or_replace", "apply_patch"})

    def __init__(
        self,
        root: str | Path,
        allow_write: bool = False,
        allow_commands: bool = False,
        command_profile: str = "minimal",
    ) -> None:
        """绑定真实工作区并选择命令白名单档位；能力开关不授予免审批权限。"""
        if command_profile not in {"minimal", "coding"}:
            raise ValueError("command_profile 必须是 minimal 或 coding")
        self.workspace = Workspace(root)
        self.root = self.workspace.root
        self.allow_write = allow_write
        self.allow_commands = allow_commands
        self.command_profile = command_profile

    def evaluate(self, call: ToolCall) -> PolicyDecision:
        """校验路径、参数和命令形态，给出 allow/ask/deny。"""
        try:
            if call.name in self.READ_TOOLS:
                self._validate_file_arguments(call.name, call.arguments, write=False)
                return PolicyDecision(PolicyAction.ALLOW, "工作区内的只读操作默认允许")
            if call.name in self.WRITE_TOOLS:
                self._validate_file_arguments(call.name, call.arguments, write=True)
                state = (
                    "已启用写能力，仍需逐次批准" if self.allow_write else "默认只读，需要显式批准"
                )
                return PolicyDecision(PolicyAction.ASK, state)
            if call.name == "run_command":
                return self._evaluate_command(call.arguments)
            return PolicyDecision(PolicyAction.DENY, f"未注册到策略的工具：{call.name}")
        except (OSError, WorkspaceViolation, ValueError, TypeError) as exc:
            return PolicyDecision(PolicyAction.DENY, str(exc))

    def decide(self, call: ToolCall) -> PolicyDecision:
        """``evaluate`` 的可读别名。"""

        return self.evaluate(call)

    def _validate_file_arguments(
        self, tool_name: str, arguments: dict[str, Any], *, write: bool
    ) -> None:
        """校验文件工具参数和路径边界；只有写工具可指向尚不存在的目标。"""
        if not isinstance(arguments, dict):
            raise TypeError("工具参数必须是对象")
        path = arguments.get("path", ".")
        if not isinstance(path, (str, os.PathLike)):
            raise TypeError("path 必须是字符串路径")
        allow_missing = write and tool_name in self.WRITE_TOOLS
        self.workspace.resolve(path, allow_missing=allow_missing)

    def _evaluate_command(self, arguments: dict[str, Any]) -> PolicyDecision:
        """检查 argv、cwd 和白名单语法；合法命令仍返回 ASK，违规命令直接 DENY。"""
        if not isinstance(arguments, dict):
            return PolicyDecision(PolicyAction.DENY, "工具参数必须是对象")
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            return PolicyDecision(PolicyAction.DENY, "argv 必须是非空字符串数组")
        if any("\x00" in item for item in argv):
            return PolicyDecision(PolicyAction.DENY, "argv 不得包含 NUL 字符")

        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, (str, os.PathLike)):
            return PolicyDecision(PolicyAction.DENY, "cwd 必须是字符串路径")
        resolved_cwd = self.workspace.resolve(cwd)
        if not resolved_cwd.is_dir():
            return PolicyDecision(PolicyAction.DENY, "命令 cwd 必须是工作区内目录")

        executable = argv[0]
        basename = Path(executable).name.lower()
        if executable != sys.executable and executable != Path(executable).name:
            return PolicyDecision(
                PolicyAction.DENY,
                "仅允许当前 Python 解释器的绝对路径，其他命令必须使用白名单名称",
            )
        dangerous = {
            "sudo",
            "curl",
            "wget",
            "rm",
            "pip",
            "pip3",
            "npm",
            "npx",
            "yarn",
            "pnpm",
            "brew",
            "apt",
            "apt-get",
        }
        if basename in dangerous:
            return PolicyDecision(PolicyAction.DENY, f"危险或联网命令始终禁止：{basename}")

        allowed = False
        python_names = {"python", "python3", Path(sys.executable).name.lower()}
        if executable == sys.executable or basename in python_names:
            allowed = self._validate_unittest_argv(argv)
        elif basename == "uv" and self.command_profile == "coding":
            allowed = self._validate_uv_argv(argv)
        elif basename == "git":
            if len(argv) >= 2 and argv[1] in {"push", "reset", "clean"}:
                return PolicyDecision(PolicyAction.DENY, f"危险 git 子命令始终禁止：{argv[1]}")
            allowed = self._validate_git_argv(argv)

        if not allowed:
            return PolicyDecision(PolicyAction.DENY, "命令不在只读/测试白名单中")
        state = (
            "已启用命令能力，仍需逐次批准" if self.allow_commands else "命令默认关闭，需要显式批准"
        )
        return PolicyDecision(
            PolicyAction.ASK,
            state,
            {"cwd": str(resolved_cwd), "argv": argv},
        )

    def _validate_uv_argv(self, argv: list[str]) -> bool:
        """允许 Coding Agent 运行少量可复现的 uv 验证命令。

        所有命令仍然返回 ASK。这里仅限制语法，不能替代进程 Sandbox；pytest
        和 unittest 都会执行工作区代码，只能在可信仓库中批准。
        """

        if len(argv) < 2:
            return False
        if argv[1:] in (["sync", "--locked"], ["sync", "--frozen"], ["lock", "--check"]):
            return True
        if argv[1:3] == ["run", "pytest"]:
            return self._validate_pytest_arguments(argv[3:])
        if argv[1:4] == ["run", "ruff", "check"]:
            return self._validate_ruff_arguments(argv[4:], format_check=False)
        if argv[1:4] == ["run", "ruff", "format"]:
            return self._validate_ruff_arguments(argv[4:], format_check=True)
        if argv[1:4] == ["run", "python", "-m"] and len(argv) >= 5:
            if argv[4] == "unittest":
                return self._validate_unittest_argv(["python", "-m", *argv[4:]])
            if argv[4] == "compileall":
                return self._validate_compileall_arguments(argv[5:])
        return False

    def _validate_pytest_arguments(self, arguments: list[str]) -> bool:
        """仅接受有限 pytest 开关与安全测试路径，拒绝额外选项。"""
        safe_flags = {"-q", "-v", "-x", "--disable-warnings", "--strict-markers"}
        for argument in arguments:
            if argument in safe_flags or argument.startswith("--maxfail="):
                continue
            if argument.startswith("-") or not self._safe_relative_path(argument):
                return False
        return True

    def _validate_ruff_arguments(self, arguments: list[str], *, format_check: bool) -> bool:
        """限制 Ruff 参数和目标路径；format 必须带 --check，不放行自动改写开关。"""
        safe_flags = {"--check"} if format_check else {"--output-format=concise"}
        saw_check = not format_check
        for argument in arguments:
            if argument in safe_flags:
                saw_check = saw_check or argument == "--check"
                continue
            if argument.startswith("-") or not self._safe_relative_path(
                argument, allow_missing=True
            ):
                return False
        return saw_check

    def _validate_compileall_arguments(self, arguments: list[str]) -> bool:
        """只允许 -q 与至少一个安全路径，避免空目标触发更广泛的默认扫描。"""
        path_count = 0
        for argument in arguments:
            if argument == "-q":
                continue
            if argument.startswith("-") or not self._safe_relative_path(argument):
                return False
            path_count += 1
        return path_count > 0

    def _safe_relative_path(self, value: str, *, allow_missing: bool = False) -> bool:
        """拒绝绝对路径与父目录穿越，再复用 Workspace 的敏感文件和真实路径检查。"""
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            return False
        try:
            self.workspace.resolve(path, allow_missing=allow_missing)
        except (OSError, WorkspaceViolation):
            return False
        return True

    def _validate_unittest_argv(self, argv: list[str]) -> bool:
        """只允许 unittest 的无路径开关和工作区内测试目标。"""

        if len(argv) < 3 or argv[1:3] != ["-m", "unittest"]:
            return False
        safe_options = {
            "-v",
            "--verbose",
            "-q",
            "--quiet",
            "-f",
            "--failfast",
            "-c",
            "--catch",
            "-b",
            "--buffer",
        }
        for argument in argv[3:]:
            if argument in safe_options:
                continue
            if argument.startswith("-"):
                return False
            path = Path(argument)
            if path.is_absolute() or ".." in path.parts:
                return False
            try:
                resolved = self.workspace.resolve(path)
            except (OSError, WorkspaceViolation):
                return False
            if not (resolved.is_dir() or (resolved.is_file() and resolved.suffix == ".py")):
                return False
        return True

    def _validate_git_argv(self, argv: list[str]) -> bool:
        """拒绝修订表达式和高级 diff 功能，只接受有限选项及安全 pathspec。"""

        if len(argv) < 2 or argv[1] not in {"status", "diff"}:
            return False
        options = {
            "status": {
                "--short",
                "--porcelain",
                "--porcelain=v1",
                "--branch",
                "--untracked-files=no",
                "--untracked-files=normal",
                "--untracked-files=all",
            },
            "diff": {"--stat", "--name-only", "--name-status", "--color=never", "--no-color"},
        }[argv[1]]
        after_separator = False
        path_count = 0
        for argument in argv[2:]:
            if argument == "--":
                if after_separator:
                    return False
                after_separator = True
                continue
            if not after_separator and argument in options:
                continue
            # diff 会把分隔符前的普通字符串解释为修订表达式；统一要求 pathspec
            # 位于 ``--`` 之后，可同时消除 HEAD、分支名和歧义参数的解析空间。
            if not after_separator:
                return False
            if argument.startswith(("-", ":")) or any(char in argument for char in "*?["):
                return False
            path = Path(argument)
            if path.is_absolute() or ".." in path.parts:
                return False
            try:
                self.workspace.resolve(path, allow_missing=True)
            except (OSError, WorkspaceViolation):
                return False
            path_count += 1
        # 裸 git diff 可能展示策略禁止直接读取的已跟踪文件，因此首版必须把
        # diff 限定到至少一个经过敏感路径检查的显式 pathspec。
        return argv[1] != "diff" or path_count > 0
