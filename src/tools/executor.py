"""统一执行入口：策略、审批、版本复核与结果归一化。"""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable

from core.control import ExecutionControl, ExecutionStopped
from core.schemas import PolicyAction, PolicyDecision, ToolCall, ToolResult
from sandbox.base import CommandRunner
from sandbox.host import HostCommandRunner
from security.policy import WorkspacePolicy
from security.secure_fs import SecureFileVersion
from tools.builtins import BuiltinTools
from tools.edit import EditTools
from tools.files import FileStorage
from tools.grep import SearchTools
from tools.preview import PreviewTools
from tools.read import ReadTools
from tools.registry import ToolRegistry
from tools.shell import CommandTools
from tools.write import WriteTools

ApprovalCallback = Callable[..., bool | str]


class ToolExecutor(
    ReadTools,
    WriteTools,
    EditTools,
    FileStorage,
    SearchTools,
    CommandTools,
    PreviewTools,
    BuiltinTools,
):
    """组合内置工具，所有模型调用须经 execute 授权。"""

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
        command_runner: CommandRunner | None = None,
    ) -> None:
        """绑定策略及资源预算；未提供注册表时装配内置工具，自定义注册表由调用方负责填充。"""
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
        self.command_runner = command_runner or HostCommandRunner(max_output_chars=max_output_chars)
        self.execution_control: ExecutionControl | None = None
        self._approved_file_version: SecureFileVersion | None = None
        self.max_files = max_files
        self.max_matches = max_matches
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes
        self.max_scan_file_bytes = max_scan_file_bytes
        self.max_scan_bytes = max_scan_bytes
        self.registry = registry or ToolRegistry()
        if registry is None:
            self._register_builtin_tools()

    def execute(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
        control: ExecutionControl | None = None,
    ) -> ToolResult:
        """统一经过策略、审批与版本复核后执行工具并返回标准结果。

        call 是模型提议，不是授权；approved 是受信宿主的显式批准，不能覆盖 DENY。
        策略拒绝、未获审批、超时和普通 handler 异常均返回 ok=False 的 ToolResult。
        文件工具可能写入磁盘，命令工具可能运行仓库代码；本层不提供 OS 沙箱。
        """
        if control is not None:
            control.checkpoint()
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
        approved_file_version: SecureFileVersion | None = None
        if decision.action is PolicyAction.ASK and not approved:
            # diff、SHA 与审批绑定版本必须来自同一稳定快照，不能分三次读取目标。
            preview, prepared_version = self._preview_with_version(call)
            approved, rejection_reason = self._request_approval(call, decision, preview)
            if not approved:
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error=f"未获批准：{rejection_reason or decision.reason}",
                    metadata=policy_metadata,
                )
            if call.name in {"write_or_replace", "apply_patch"} and not isinstance(
                prepared_version, SecureFileVersion
            ):
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error=(
                        f"审批预览未取得安全文件版本，已 fail closed：{preview or '无可用预览'}"
                    ),
                    metadata=policy_metadata,
                )
            if prepared_version != self._write_version(call):
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error="审批后目标文件发生变化，已拒绝执行；请重新预览并审批",
                    metadata=policy_metadata,
                )
            if isinstance(prepared_version, SecureFileVersion):
                approved_file_version = prepared_version

        # 只有过完策略和审批门槛，才从注册表取出真实函数并展开参数调用。
        # 审批可能阻塞较长时间，必须在任何真实 handler/副作用前重新检查生命周期。
        if control is not None:
            control.checkpoint()
        handler = self.registry.get(call.name)
        if handler is None:
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"未知工具：{call.name}",
                metadata=policy_metadata,
            )
        previous_control = self.execution_control
        previous_approved_file_version = self._approved_file_version
        self.execution_control = control
        self._approved_file_version = approved_file_version
        try:
            output = handler(**call.arguments)
            ok = not (
                call.name == "run_command"
                and isinstance(output, dict)
                and (output.get("returncode") != 0 or output.get("cleanup_failed") is True)
            )
            error = None
            if not ok:
                error = (
                    "命令或沙箱清理失败"
                    if output.get("cleanup_failed") is True
                    else f"命令退出码为 {output.get('returncode')}"
                )
            return ToolResult(
                call.id,
                call.name,
                ok,
                output=output,
                error=error,
                metadata=policy_metadata,
            )
        except ExecutionStopped:
            raise
        except subprocess.TimeoutExpired as exc:
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"命令执行超时（{exc.timeout} 秒）",
                metadata=policy_metadata,
            )
        except Exception as exc:  # 注册表允许扩展工具，任何普通执行错误都应回传给 Agent。
            failure_metadata = dict(policy_metadata)
            if bool(getattr(exc, "cleanup_failed", False)):
                failure_metadata["sandbox"] = {
                    "backend": str(getattr(exc, "backend", self.command_runner.backend)),
                    "cleanup_failed": True,
                }
            return ToolResult(
                call.id,
                call.name,
                False,
                error=f"工具执行失败：{exc}",
                metadata=failure_metadata,
            )
        finally:
            self.execution_control = previous_control
            self._approved_file_version = previous_approved_file_version

    def _request_approval(
        self,
        call: ToolCall,
        decision: PolicyDecision,
        preview: str | None,
    ) -> tuple[bool, str | None]:
        """兼容旧布尔回调；字符串表示拒绝并向 Agent 返回可操作原因。"""
        callback = self.approval_callback
        if callback is None:
            return False, None
        try:
            parameters = inspect.signature(callback).parameters
            if len(parameters) >= 3:
                result = callback(call, decision, preview)
            elif len(parameters) >= 2:
                result = callback(call, decision)
            else:
                result = callback(call)
            if result is True:
                return True, None
            if isinstance(result, str) and result.strip():
                return False, result.strip()[:1000]
            return False, None
        except (TypeError, ValueError):
            return False, None


def create_default_registry(executor: ToolExecutor) -> ToolRegistry:
    """返回执行器已绑定的内置注册表，便于调用方发现工具。"""

    return executor.registry
