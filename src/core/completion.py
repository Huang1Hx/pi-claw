"""基于实际 ToolResult 的宿主侧完成验收。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.schemas import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """一次工具请求及其真实执行结果。"""

    call: ToolCall
    result: ToolResult


@dataclass(frozen=True, slots=True)
class CompletionCheck:
    """宿主对模型完成声明的判定与证据摘要。"""

    accepted: bool
    reason: str
    changed_files: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的数据对象。"""
        return asdict(self)


@runtime_checkable
class CompletionVerifier(Protocol):
    """宿主完成验收扩展协议。"""

    def verify(
        self,
        *,
        task: str,
        final_text: str,
        observations: Sequence[ToolObservation],
    ) -> CompletionCheck:
        """根据宿主已观测到的工具结果判定完成候选。

        task/final_text 提供任务和候选结论，observations 按实际执行顺序提供证据。
        返回 accepted、原因和证据摘要；拒绝会反馈给模型继续处理，而非直接宣告成功。
        实现应只检查已有证据，不在验收回调里绕开 ToolExecutor 执行新操作。
        """
        ...


class ToolEvidenceCompletionVerifier:
    """要求成功写入之后出现成功的受认可验证命令。

    该验收器不自行执行命令，因此不会扩大 Agent 权限。它只检查已经经过
    Policy/Approval 并形成 ToolResult 的证据。没有成功写入的只读或拒绝场景
    可以正常结束；这只表示无需代码变更验收，不验证最终自然语言的全部事实。
    """

    WRITE_TOOLS = frozenset({"write_or_replace", "apply_patch"})

    def __init__(
        self,
        *,
        accepted_kinds: frozenset[str] | None = None,
    ) -> None:
        """配置可接受的验证类别；默认认可测试、静态检查、编译和锁文件检查。"""
        self.accepted_kinds = (
            frozenset({"test", "lint", "compile", "lock"})
            if accepted_kinds is None
            else accepted_kinds
        )
        if not self.accepted_kinds:
            raise ValueError("accepted_kinds 不能为空")

    def verify(
        self,
        *,
        task: str,
        final_text: str,
        observations: Sequence[ToolObservation],
    ) -> CompletionCheck:
        """根据真实工具结果检查完成声明是否具备最低验证证据。"""
        del task, final_text
        successful_writes = [
            (index, observation)
            for index, observation in enumerate(observations)
            if observation.call.name in self.WRITE_TOOLS and observation.result.ok
        ]
        if not successful_writes:
            return CompletionCheck(
                True,
                "本回合没有成功文件写入，无需代码变更验收",
            )

        # 验证必须晚于最后一次成功写入；此前的绿灯不能为后续修改背书。
        last_write_index = successful_writes[-1][0]
        changed_files = tuple(
            dict.fromkeys(
                str(observation.call.arguments.get("path"))
                for _, observation in successful_writes
                if isinstance(observation.call.arguments.get("path"), str)
            )
        )
        checks: list[dict[str, Any]] = []
        for observation in observations[last_write_index + 1 :]:
            if observation.call.name != "run_command":
                continue
            argv = observation.call.arguments.get("argv")
            kind = self._verification_kind(argv)
            if kind is None or kind not in self.accepted_kinds:
                continue
            output = observation.result.output
            returncode = output.get("returncode") if isinstance(output, dict) else None
            ok = observation.result.ok and returncode == 0
            checks.append(
                {
                    "kind": kind,
                    "ok": ok,
                    "argv": list(argv),
                    "returncode": returncode,
                }
            )

        # 这是“至少一项认可检查通过”的最低证据门槛，不是全部业务需求的正确性证明。
        if any(check["ok"] for check in checks):
            successful_kinds = sorted({str(check["kind"]) for check in checks if check["ok"]})
            return CompletionCheck(
                True,
                f"写入后已有成功验证证据：{', '.join(successful_kinds)}",
                changed_files,
                tuple(checks),
            )
        if checks:
            reason = "写入后的受认可验证命令均未通过"
        else:
            reason = "成功写入后没有运行受认可的测试、静态检查、编译或锁文件检查"
        return CompletionCheck(False, reason, changed_files, tuple(checks))

    @staticmethod
    def _verification_kind(argv: Any) -> str | None:
        """按 argv 结构识别验证类别，无法识别时返回 None；这里只分类，不执行命令。"""
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            return None
        executable = Path(argv[0]).name.lower()
        arguments = argv[1:]
        if executable == "uv":
            if arguments[:2] == ["run", "pytest"]:
                return "test"
            if arguments[:4] == ["run", "python", "-m", "unittest"]:
                return "test"
            if arguments[:3] == ["run", "ruff", "check"]:
                return "lint"
            if arguments[:3] == ["run", "ruff", "format"] and "--check" in arguments[3:]:
                return "lint"
            if arguments[:4] == ["run", "python", "-m", "compileall"]:
                return "compile"
            if arguments == ["lock", "--check"]:
                return "lock"
            return None
        if len(arguments) >= 2 and arguments[:2] == ["-m", "unittest"]:
            return "test"
        if len(arguments) >= 2 and arguments[:2] == ["-m", "compileall"]:
            return "compile"
        return None
