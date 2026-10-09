"""阶段 3 新任务 holdout：实现冻结后一次性加入、未参与本阶段调试的中等难度任务。

这些任务与核心 10 项同协议、同模型、同评分器，但只在 holdout 批次中使用，不计入
默认 90-trial 回归，避免“用调试过的题追分”。参考实现必须满足受限纯函数语法约束。
"""

from __future__ import annotations

from evaluation.mode_tasks import TASKS, build_task

HOLDOUT_TASK_DEFINITIONS: dict[str, dict] = {
    "merge_sorted_streams": {
        "title": "多个有序流的合并逻辑",
        "function_name": "merge_sorted_streams",
        "parameters": ("streams",),
        "spec": """# 有序流合并规则

实现 `merge_sorted_streams(streams)`：

- streams 必须是 list；每个 stream 必须是 list，元素为非 bool 的 int。
- 每个 stream 必须按非递减顺序排列；否则视为无效输入。
- 返回把全部元素按非递减顺序合并后的新列表，重复值原样保留。
- 任一输入无效时返回空列表，不抛异常。
- 不得修改 streams 及其内部列表。
""",
        "initial": '''def merge_sorted_streams(streams):
    """Merge pre-sorted streams."""
    merged = []
    for stream in streams:
        for value in stream:
            merged.append(value)
    merged.sort()
    return merged
''',
        "reference": '''def merge_sorted_streams(streams):
    """Validate sorted streams and merge them without touching the input."""
    if not isinstance(streams, list):
        return []
    merged = []
    for stream in streams:
        if not isinstance(stream, list):
            return []
        previous = None
        for value in stream:
            if (not isinstance(value, int) or isinstance(value, bool)
                    or previous is not None and value < previous):
                return []
            previous = value
            merged.append(value)
    merged.sort()
    return merged
''',
        "public_test": """import unittest
from solution import merge_sorted_streams


class MergeSortedStreamsTests(unittest.TestCase):
    def test_merges_sorted_streams(self):
        self.assertEqual(merge_sorted_streams([[1, 4], [2, 3]]), [1, 2, 3, 4])

    def test_empty_input(self):
        self.assertEqual(merge_sorted_streams([]), [])

    def test_unsorted_stream_is_invalid(self):
        self.assertEqual(merge_sorted_streams([[3, 1]]), [])


if __name__ == "__main__":
    unittest.main()
""",
    },
    "run_length_encode": {
        "title": "相邻游程编码逻辑",
        "function_name": "run_length_encode",
        "parameters": ("values",),
        "spec": """# 游程编码规则

实现 `run_length_encode(values)`：

- values 必须是 list；每个元素必须是 str、int 或 bool。
- 把相邻且相等的元素编码为 `(value, count)` 元组，count 为正整数。
- 仅相邻元素合并；位置不相邻的相同值必须各自产生独立元组。
- 空列表返回空列表；任一输入无效时返回空列表，不抛异常。
- 不得修改 values。
""",
        "initial": '''def run_length_encode(values):
    """Encode values by counting occurrences."""
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    result = []
    for value in counts:
        result.append((value, counts[value]))
    return result
''',
        "reference": '''def run_length_encode(values):
    """Validate values and encode consecutive runs without mutation."""
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if not isinstance(value, (str, int, bool)):
            return []
        if result and result[-1][0] == value:
            result[-1] = (value, result[-1][1] + 1)
        else:
            result.append((value, 1))
    return result
''',
        "public_test": """import unittest
from solution import run_length_encode


class RunLengthEncodeTests(unittest.TestCase):
    def test_encodes_runs(self):
        self.assertEqual(run_length_encode(["a", "a", "b"]), [("a", 2), ("b", 1)])

    def test_empty_input(self):
        self.assertEqual(run_length_encode([]), [])

    def test_non_adjacent_values_stay_separate(self):
        self.assertEqual(run_length_encode([1, 2, 1]), [(1, 1), (2, 1), (1, 1)])


if __name__ == "__main__":
    unittest.main()
""",
    },
    "rolling_max": {
        "title": "滑动窗口最大值逻辑",
        "function_name": "rolling_max",
        "parameters": ("values", "k"),
        "spec": """# 滑动窗口最大值规则

实现 `rolling_max(values, k)`：

- values 必须是 list，元素为非 bool 的 int。
- k 必须是正整数（bool 不算整数）。
- 返回与 values 等长的新列表：第 i 项是 values 中截至 i、长度不超过 k 的窗口内最大值。
- 空列表返回空列表；任一输入无效时返回空列表，不抛异常。
- 不得修改 values。
""",
        "initial": '''def rolling_max(values, k):
    """Compute the maximum over the most recent values."""
    result = []
    window = []
    for value in values:
        window.append(value)
        best = window[0]
        for item in window:
            if item > best:
                best = item
        result.append(best)
        if len(window) == k:
            window = []
    return result
''',
        "reference": '''def rolling_max(values, k):
    """Validate inputs and compute overlapping-window maxima without mutation."""
    if (not isinstance(values, list) or not isinstance(k, int)
            or isinstance(k, bool) or k <= 0):
        return []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool):
            return []
    result = []
    window = []
    for value in values:
        window.append(value)
        if len(window) > k:
            trimmed = []
            start = len(window) - k
            index = 0
            for item in window:
                if index >= start:
                    trimmed.append(item)
                index = index + 1
            window = trimmed
        best = window[0]
        for item in window:
            if item > best:
                best = item
        result.append(best)
    return result
''',
        "public_test": """import unittest
from solution import rolling_max


class RollingMaxTests(unittest.TestCase):
    def test_full_window_maximum(self):
        self.assertEqual(rolling_max([1, 3, 2], 3), [1, 3, 3])

    def test_single_slot_window(self):
        self.assertEqual(rolling_max([4, 1, 9], 1), [4, 1, 9])

    def test_zero_window_is_invalid(self):
        self.assertEqual(rolling_max([1], 0), [])


if __name__ == "__main__":
    unittest.main()
""",
    },
}


HOLDOUT_TASKS = {
    task_id: build_task(task_id, definition)
    for task_id, definition in HOLDOUT_TASK_DEFINITIONS.items()
}

_OVERLAP = set(TASKS) & set(HOLDOUT_TASKS)
if _OVERLAP:
    raise ValueError(f"holdout 任务不得与核心任务重名：{sorted(_OVERLAP)}")

ALL_TASKS = {**TASKS, **HOLDOUT_TASKS}
