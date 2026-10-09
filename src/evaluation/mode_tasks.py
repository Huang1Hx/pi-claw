"""把三种 Agent 架构共用的任务目录转换为强类型任务对象。"""

from __future__ import annotations

from dataclasses import dataclass

from evaluation.mode_task_catalog import TASK_DEFINITIONS


@dataclass(frozen=True, slots=True)
class ModeTask:
    """一个可复制到独立工作区、可被统一评分的纯函数任务。"""

    task_id: str
    title: str
    prompt: str
    function_name: str
    parameters: tuple[str, ...]
    defaults: tuple[object, ...]
    initial: str
    reference: str
    public_test: str
    spec: str

    @property
    def files(self) -> dict[str, str]:
        """返回 Agent 可见的固定起始文件，隐藏用例不会泄漏到工作区。"""
        return {
            "solution.py": self.initial,
            "test_target.py": self.public_test,
            "SPEC.md": self.spec,
        }


def _prompt(title: str) -> str:
    """三个架构使用完全相同的主任务文字，避免提示差异污染对照。"""
    return (
        f"修复 {title}。先阅读 SPEC.md、solution.py 和 test_target.py，只修改 solution.py。"
        "实现完整规则，不能修改公开测试或新增文件。运行且仅运行 "
        "uv run python -m unittest -q test_target.py 验证，并依据真实结果报告。"
        "外部评分还会覆盖未公开的边界输入。为安全评分，代码不得导入模块、递归、使用推导式或"
        "执行顶层语句；可使用不超过三个纯辅助函数、if/for/赋值/return、基础比较运算、"
        "isinstance/len/sorted/dict/list/tuple/max/min，以及 copy/get/items/append/sort。"
        "sort/sorted 的 key 仅可使用单参数 lambda。"
    )


def build_task(task_id: str, definition: dict) -> ModeTask:
    """从只含静态数据的目录构造任务，并集中补齐公平性提示。"""
    return ModeTask(
        task_id=task_id,
        title=definition["title"],
        prompt=_prompt(definition["title"]),
        function_name=definition["function_name"],
        parameters=tuple(definition["parameters"]),
        defaults=tuple(definition.get("defaults", ())),
        initial=definition["initial"],
        reference=definition["reference"],
        public_test=definition["public_test"],
        spec=definition["spec"],
    )


# 字典顺序即真实评测运行顺序；各模式和各重复试验共享这一顺序。
TASKS = {
    task_id: build_task(task_id, definition) for task_id, definition in TASK_DEFINITIONS.items()
}
