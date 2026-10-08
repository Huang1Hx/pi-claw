"""基于实际 ToolResult 的宿主侧完成验收。"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from core.schemas import ToolCall, ToolResult

VERIFICATION_KINDS = frozenset({"test", "lint", "compile", "lock"})


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


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """宿主声明的完成验收计划；模型不能修改它来降低验收要求。

    ``required_kinds`` 是变更后必须出现的成功检查类别，``optional_kinds`` 是可接受但不强制的
    类别，``protected_patterns`` 是不允许写入的文件模式（如测试文件），``change_to_tests`` 把
    变更文件映射到必须真实运行的关联测试，用于拒绝无关测试绿灯。
    """

    required_kinds: frozenset[str]
    optional_kinds: frozenset[str] = frozenset()
    protected_patterns: tuple[str, ...] = ()
    change_to_tests: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        """拒绝空必跑、未知类别、必跑/可选重叠、空模式与重复映射，避免门槛被静默降低。"""
        if not isinstance(self.required_kinds, frozenset) or not self.required_kinds:
            raise ValueError("required_kinds（必跑检查）必须是非空 frozenset")
        if any(kind not in VERIFICATION_KINDS for kind in self.required_kinds):
            raise ValueError("required_kinds 含未知类别")
        if any(kind not in VERIFICATION_KINDS for kind in self.optional_kinds):
            raise ValueError("optional_kinds 含未知类别")
        if self.required_kinds & self.optional_kinds:
            raise ValueError("必跑与可选检查必须不相交")
        if any(not isinstance(pattern, str) or not pattern for pattern in self.protected_patterns):
            raise ValueError("protected_patterns 必须是非空字符串")
        seen: set[str] = set()
        for path, tests in self.change_to_tests:
            if not isinstance(path, str) or not path:
                raise ValueError("change_to_tests 的变更文件必须是非空字符串")
            if path in seen:
                raise ValueError(f"change_to_tests 变更文件重复：{path}")
            seen.add(path)
            if (
                not isinstance(tests, tuple)
                or not tests
                or any(not isinstance(test, str) or not test for test in tests)
            ):
                raise ValueError("change_to_tests 的关联测试必须是非空字符串元组")

    def is_protected(self, path: str) -> bool:
        """按 basename 或相对路径匹配受保护模式。"""
        name = PurePosixPath(path).name
        return any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern)
            for pattern in self.protected_patterns
        )

    def relevant_tests(self, path: str) -> tuple[str, ...]:
        """返回某个变更文件声明的关联测试；未声明时返回空元组（不做关联检查）。"""
        for key, tests in self.change_to_tests:
            if key == path:
                return tests
        return ()


def _test_files_in_argv(argv: object) -> tuple[str, ...]:
    """从运行参数中提取看起来像测试文件的路径；只做分类，不执行命令。"""
    if not isinstance(argv, list):
        return ()
    found: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item.endswith(".py"):
            continue
        name = PurePosixPath(item).name
        if name.startswith("test_") or name.endswith("_test.py"):
            found.append(item)
    return tuple(dict.fromkeys(found))


class PlannedCompletionVerifier:
    """按宿主声明的 ``VerificationPlan`` 验收完成，不执行新命令、不扩大 Agent 权限。

    与 ``ToolEvidenceCompletionVerifier`` 相比，它额外拒绝写入受保护文件（测试篡改/删除）、
    要求每个必跑类别在最后一次成功写入后通过，并在声明映射时拒绝无关测试绿灯。
    """

    WRITE_TOOLS = frozenset({"write_or_replace", "apply_patch"})

    def __init__(self, plan: VerificationPlan) -> None:
        """接收不可变计划；非法类型直接报错而不是退化成宽松验收。"""
        if not isinstance(plan, VerificationPlan):
            raise TypeError("plan 必须是 VerificationPlan")
        self.plan = plan

    def verify(
        self,
        *,
        task: str,
        final_text: str,
        observations: Sequence[ToolObservation],
    ) -> CompletionCheck:
        """根据真实工具证据检查完成声明；拒绝会反馈给模型继续处理。"""
        del task, final_text
        successful_writes = [
            (index, observation)
            for index, observation in enumerate(observations)
            if observation.call.name in self.WRITE_TOOLS and observation.result.ok
        ]
        if not successful_writes:
            return CompletionCheck(True, "本回合没有成功文件写入，无需代码变更验收")

        changed_files = tuple(
            dict.fromkeys(
                str(observation.call.arguments.get("path"))
                for _, observation in successful_writes
                if isinstance(observation.call.arguments.get("path"), str)
            )
        )
        protected = tuple(path for path in changed_files if self.plan.is_protected(path))
        if protected:
            return CompletionCheck(
                False,
                f"写入受保护文件（疑似测试篡改或删除）：{', '.join(protected)}",
                changed_files,
            )

        last_write_index = successful_writes[-1][0]
        checks: list[dict[str, Any]] = []
        passing_tests: list[tuple[str, ...]] = []
        for observation in observations[last_write_index + 1 :]:
            if observation.call.name != "run_command":
                continue
            argv = observation.call.arguments.get("argv")
            kind = ToolEvidenceCompletionVerifier._verification_kind(argv)
            if kind is None:
                continue
            output = observation.result.output
            returncode = output.get("returncode") if isinstance(output, dict) else None
            ok = observation.result.ok and returncode == 0
            test_files = _test_files_in_argv(argv) if kind == "test" else ()
            checks.append(
                {
                    "kind": kind,
                    "ok": ok,
                    "argv": list(argv) if isinstance(argv, list) else [],
                    "returncode": returncode,
                    "test_files": list(test_files),
                }
            )
            if ok and kind == "test":
                passing_tests.append(test_files)

        passed_kinds = {check["kind"] for check in checks if check["ok"]}
        missing = sorted(self.plan.required_kinds - passed_kinds)
        if missing:
            return CompletionCheck(
                False,
                "成功写入后缺少必跑检查：" + ", ".join(missing),
                changed_files,
                tuple(checks),
            )

        if self.plan.change_to_tests:
            unlinked: list[str] = []
            for path in changed_files:
                tests = self.plan.relevant_tests(path)
                if not tests:
                    continue
                if not any(set(files) & set(tests) for files in passing_tests):
                    unlinked.append(f"{path}→{','.join(tests)}")
            if unlinked:
                return CompletionCheck(
                    False,
                    "变更文件缺少关联测试绿灯：" + "; ".join(unlinked),
                    changed_files,
                    tuple(checks),
                )

        successful_kinds = sorted(passed_kinds)
        return CompletionCheck(
            True,
            f"写入后满足必跑检查：{', '.join(successful_kinds)}",
            changed_files,
            tuple(checks),
        )
