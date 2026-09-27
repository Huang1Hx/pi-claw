"""三种 Agent 架构共用的中等复杂度纯函数任务；公开测试与隐藏行为边界分离。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModeTask:
    """一个可复制到独立工作区的任务定义。"""

    task_id: str
    title: str
    prompt: str
    function_name: str
    initial: str
    reference: str
    public_test: str
    spec: str

    @property
    def files(self) -> dict[str, str]:
        """返回 Agent 可见的固定起始文件。"""
        return {
            "solution.py": self.initial,
            "test_target.py": self.public_test,
            "SPEC.md": self.spec,
        }


INVENTORY_SPEC = """# 库存预留规则

实现 `reserve(stock, orders)`：

- `stock` 是 `str -> 非负 int` 字典；`orders` 是 `(sku, quantity)` 二元组列表。
- sku 必须是非空字符串；quantity 必须是正整数，bool 不算整数。
- 任一输入、订单、sku、quantity 或库存数量无效时，返回 `(False, 原库存的浅拷贝)`；
  stock 本身不是字典时返回 `(False, {})`。
- 未知 sku 或相同 sku 累计需求超过库存，也必须原子失败并返回未扣减的库存副本。
- 全部有效且库存足够时返回 `(True, 扣减后的新字典)`。
- 任何路径都不得修改传入的 stock 或 orders。
"""

INVENTORY_INITIAL = '''def reserve(stock, orders):
    """Reserve stock for a batch of orders."""
    remaining = stock.copy()
    for sku, quantity in orders:
        if sku not in remaining or remaining[sku] < quantity:
            return False, remaining
        remaining[sku] -= quantity
    return True, remaining
'''

INVENTORY_REFERENCE = '''def reserve(stock, orders):
    """Atomically reserve a validated order batch without mutating inputs."""
    if not isinstance(stock, dict):
        return False, {}
    original = stock.copy()
    for sku, quantity in stock.items():
        if (not isinstance(sku, str) or not sku or not isinstance(quantity, int)
                or isinstance(quantity, bool) or quantity < 0):
            return False, original
    if not isinstance(orders, list):
        return False, original
    needed = {}
    for order in orders:
        if not isinstance(order, tuple) or len(order) != 2:
            return False, original
        sku, quantity = order
        if (not isinstance(sku, str) or not sku or not isinstance(quantity, int)
                or isinstance(quantity, bool) or quantity <= 0):
            return False, original
        if sku not in original:
            return False, original
        needed[sku] = needed.get(sku, 0) + quantity
        if needed[sku] > original[sku]:
            return False, original
    remaining = original.copy()
    for sku, quantity in needed.items():
        remaining[sku] -= quantity
    return True, remaining
'''

INVENTORY_TEST = """import unittest
from solution import reserve


class ReserveTests(unittest.TestCase):
    def test_success_does_not_mutate(self):
        stock = {"apple": 5, "pear": 2}
        self.assertEqual(reserve(stock, [("apple", 2)]), (True, {"apple": 3, "pear": 2}))
        self.assertEqual(stock, {"apple": 5, "pear": 2})

    def test_unknown_sku_is_atomic(self):
        stock = {"apple": 5}
        self.assertEqual(reserve(stock, [("apple", 2), ("pear", 1)]), (False, {"apple": 5}))

    def test_duplicate_orders_use_cumulative_quantity(self):
        self.assertEqual(reserve({"apple": 3}, [("apple", 2), ("apple", 2)]), (False, {"apple": 3}))

    def test_empty_batch_succeeds(self):
        self.assertEqual(reserve({"apple": 1}, []), (True, {"apple": 1}))


if __name__ == "__main__":
    unittest.main()
"""

WINDOWS_SPEC = """# 时间窗口合并规则

实现 `merge_windows(windows, gap=0)`：

- windows 必须是 list；每项必须是长度为 2 的 list 或 tuple，端点为 int 且不能是 bool。
- 每个窗口满足 `start < end`；gap 必须是非负 int 且不能是 bool。
- 任一输入无效时返回空列表，不抛异常。
- 先按起点、终点升序，再合并重叠、接触或间距不超过 gap 的窗口。
- 输出始终是 `(start, end)` tuple 的新列表，不得修改输入及其内部列表。
"""

WINDOWS_INITIAL = '''def merge_windows(windows, gap=0):
    """Merge overlapping windows."""
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
'''

WINDOWS_REFERENCE = '''def merge_windows(windows, gap=0):
    """Validate, sort and merge windows without changing the caller's input."""
    if (not isinstance(windows, list) or not isinstance(gap, int)
            or isinstance(gap, bool) or gap < 0):
        return []
    cleaned = []
    for window in windows:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            return []
        start, end = window
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool) or start >= end):
            return []
        cleaned.append((start, end))
    merged = []
    for start, end in sorted(cleaned):
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
'''

WINDOWS_TEST = """import unittest
from solution import merge_windows


class MergeWindowsTests(unittest.TestCase):
    def test_unsorted_overlap(self):
        self.assertEqual(merge_windows([(5, 8), (1, 3), (2, 6)]), [(1, 8)])

    def test_gap_connects_windows(self):
        self.assertEqual(merge_windows([(1, 2), (4, 6), (9, 10)], gap=2), [(1, 6), (9, 10)])

    def test_invalid_window_returns_empty(self):
        self.assertEqual(merge_windows([(3, 3)]), [])

    def test_input_is_not_modified(self):
        source = [[5, 7], [1, 2]]
        before = [item.copy() for item in source]
        self.assertEqual(merge_windows(source), [(1, 2), (5, 7)])
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
"""


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


TASKS = {
    "inventory_reservation": ModeTask(
        "inventory_reservation",
        "批量库存原子预留",
        _prompt("批量库存原子预留逻辑"),
        "reserve",
        INVENTORY_INITIAL,
        INVENTORY_REFERENCE,
        INVENTORY_TEST,
        INVENTORY_SPEC,
    ),
    "window_merge": ModeTask(
        "window_merge",
        "带容差的时间窗口合并",
        _prompt("带容差的时间窗口合并逻辑"),
        "merge_windows",
        WINDOWS_INITIAL,
        WINDOWS_REFERENCE,
        WINDOWS_TEST,
        WINDOWS_SPEC,
    ),
}
