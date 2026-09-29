"""阶段 2 的十项中等复杂度任务目录；这里只保存 Agent 可见的静态材料。"""

from __future__ import annotations

TASK_DEFINITIONS = {
    "inventory_reservation": {
        "title": "批量库存原子预留逻辑",
        "function_name": "reserve",
        "parameters": ("stock", "orders"),
        "spec": """# 库存预留规则

实现 `reserve(stock, orders)`：

- `stock` 是 `str -> 非负 int` 字典；`orders` 是 `(sku, quantity)` 二元组列表。
- sku 必须是非空字符串；quantity 必须是正整数，bool 不算整数。
- 任一输入、订单、sku、quantity 或库存数量无效时，返回 `(False, 原库存的浅拷贝)`；
  stock 本身不是字典时返回 `(False, {})`。
- 未知 sku 或相同 sku 累计需求超过库存，也必须原子失败并返回未扣减的库存副本。
- 全部有效且库存足够时返回 `(True, 扣减后的新字典)`。
- 任何路径都不得修改传入的 stock 或 orders。
""",
        "initial": '''def reserve(stock, orders):
    """Reserve stock for a batch of orders."""
    remaining = stock.copy()
    for sku, quantity in orders:
        if sku not in remaining or remaining[sku] < quantity:
            return False, remaining
        remaining[sku] -= quantity
    return True, remaining
''',
        "reference": '''def reserve(stock, orders):
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
''',
        "public_test": """import unittest
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
""",
    },
    "window_merge": {
        "title": "带容差的时间窗口合并逻辑",
        "function_name": "merge_windows",
        "parameters": ("windows", "gap"),
        "defaults": (0,),
        "spec": """# 时间窗口合并规则

实现 `merge_windows(windows, gap=0)`：

- windows 必须是 list；每项必须是长度为 2 的 list 或 tuple，端点为 int 且不能是 bool。
- 每个窗口满足 `start < end`；gap 必须是非负 int 且不能是 bool。
- 任一输入无效时返回空列表，不抛异常。
- 先按起点、终点升序，再合并重叠、接触或间距不超过 gap 的窗口。
- 输出始终是 `(start, end)` tuple 的新列表，不得修改输入及其内部列表。
""",
        "initial": '''def merge_windows(windows, gap=0):
    """Merge overlapping windows."""
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
''',
        "reference": '''def merge_windows(windows, gap=0):
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
''',
        "public_test": """import unittest
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
""",
    },
    "permission_filter": {
        "title": "角色权限请求过滤逻辑",
        "function_name": "allowed_actions",
        "parameters": ("role", "requested", "grants"),
        "spec": """# 权限过滤规则

实现 `allowed_actions(role, requested, grants)`：

- role 必须是非空字符串；requested 是动作字符串列表；grants 是 `(role, action)` 二元组列表。
- grant 中角色和动作都必须是非空字符串，且完全相同的授权不能重复。
- role 未出现在 grants、任一结构或值无效时返回空列表。
- 返回 requested 中获得授权的动作，保持首次请求顺序并去重。
- 不得修改任何输入。
""",
        "initial": '''def allowed_actions(role, requested, grants):
    """Return requested actions granted to a role."""
    allowed = []
    for grant_role, action in grants:
        if grant_role == role:
            allowed.append(action)
    result = []
    for action in requested:
        if action in allowed:
            result.append(action)
    return result
''',
        "reference": '''def allowed_actions(role, requested, grants):
    """Validate grants and return unique authorized actions in request order."""
    if (not isinstance(role, str) or not role or not isinstance(requested, list)
            or not isinstance(grants, list)):
        return []
    known_roles = []
    seen_grants = []
    allowed = []
    for grant in grants:
        if not isinstance(grant, tuple) or len(grant) != 2:
            return []
        grant_role, action = grant
        if (not isinstance(grant_role, str) or not grant_role
                or not isinstance(action, str) or not action or grant in seen_grants):
            return []
        seen_grants.append(grant)
        if grant_role not in known_roles:
            known_roles.append(grant_role)
        if grant_role == role:
            allowed.append(action)
    if role not in known_roles:
        return []
    result = []
    for action in requested:
        if not isinstance(action, str) or not action:
            return []
        if action in allowed and action not in result:
            result.append(action)
    return result
''',
        "public_test": """import unittest
from solution import allowed_actions


class AllowedActionsTests(unittest.TestCase):
    def test_filters_and_preserves_request_order(self):
        grants = [("admin", "read"), ("user", "read"), ("admin", "write")]
        self.assertEqual(
            allowed_actions("admin", ["write", "delete", "read"], grants),
            ["write", "read"],
        )

    def test_repeated_request_is_deduplicated(self):
        self.assertEqual(allowed_actions("user", ["read", "read"], [("user", "read")]), ["read"])

    def test_unknown_role_is_empty(self):
        self.assertEqual(allowed_actions("guest", ["read"], [("user", "read")]), [])


if __name__ == "__main__":
    unittest.main()
""",
    },
    "balance_transfer": {
        "title": "账户批量转账原子处理逻辑",
        "function_name": "apply_transfers",
        "parameters": ("balances", "transfers"),
        "spec": """# 批量转账规则

实现 `apply_transfers(balances, transfers)`：

- balances 是 `非空账户名 -> 非负 int` 字典；bool 不算整数。
- transfers 是 `(source, target, amount)` 三元组列表，账户必须存在且不同，amount 为正整数。
- 转账按顺序执行，后续转账可以使用前序收到的余额。
- 任一步余额不足或输入无效时原子失败：返回 `(False, 原余额浅拷贝)`。
- balances 本身不是字典时返回 `(False, {})`；成功时返回 `(True, 新余额)`。
- 不得修改输入。
""",
        "initial": '''def apply_transfers(balances, transfers):
    """Apply transfers in order."""
    result = balances.copy()
    for source, target, amount in transfers:
        if source not in result or target not in result or result[source] < amount:
            return False, result
        result[source] -= amount
        result[target] += amount
    return True, result
''',
        "reference": '''def apply_transfers(balances, transfers):
    """Atomically apply a validated sequence of balance transfers."""
    if not isinstance(balances, dict):
        return False, {}
    original = balances.copy()
    for account, balance in balances.items():
        if (not isinstance(account, str) or not account or not isinstance(balance, int)
                or isinstance(balance, bool) or balance < 0):
            return False, original
    if not isinstance(transfers, list):
        return False, original
    result = original.copy()
    for transfer in transfers:
        if not isinstance(transfer, tuple) or len(transfer) != 3:
            return False, original
        source, target, amount = transfer
        if (not isinstance(source, str) or not source or not isinstance(target, str)
                or not target or source == target or not isinstance(amount, int)
                or isinstance(amount, bool) or amount <= 0):
            return False, original
        if source not in result or target not in result or result[source] < amount:
            return False, original
        result[source] -= amount
        result[target] += amount
    return True, result
''',
        "public_test": """import unittest
from solution import apply_transfers


class ApplyTransfersTests(unittest.TestCase):
    def test_later_transfer_uses_received_balance(self):
        balances = {"a": 5, "b": 0, "c": 0}
        self.assertEqual(
            apply_transfers(balances, [("a", "b", 4), ("b", "c", 3)]),
            (True, {"a": 1, "b": 1, "c": 3}),
        )
        self.assertEqual(balances, {"a": 5, "b": 0, "c": 0})

    def test_late_failure_is_atomic(self):
        self.assertEqual(
            apply_transfers({"a": 5, "b": 0}, [("a", "b", 2), ("a", "b", 9)]),
            (False, {"a": 5, "b": 0}),
        )

    def test_empty_batch_succeeds(self):
        self.assertEqual(apply_transfers({"a": 1}, []), (True, {"a": 1}))


if __name__ == "__main__":
    unittest.main()
""",
    },
    "dependency_order": {
        "title": "稳定依赖拓扑排序逻辑",
        "function_name": "dependency_order",
        "parameters": ("graph",),
        "spec": """# 依赖排序规则

实现 `dependency_order(graph)`：

- graph 是 `非空节点名 -> 依赖节点名列表` 字典；所有依赖必须是已有节点。
- 节点名和依赖名必须是非空字符串；单个节点的依赖不能重复。
- 返回满足依赖顺序的节点列表；同时可选的节点按字典序选择，结果必须确定。
- 循环、自依赖、缺失依赖或任一无效输入均返回空列表。
- 空字典合法并返回空列表，不得修改输入。
- 为保证评分执行有界，禁止 `while`、递归、import、集合和各种推导式；请使用最多按节点数
  执行的有界 `for` 循环。`for` 内允许使用 `break` 和 `continue`。
""",
        "initial": '''def dependency_order(graph):
    """Return nodes in a deterministic order."""
    return sorted(graph)
''',
        "reference": '''def dependency_order(graph):
    """Validate a dependency graph and produce a stable topological order."""
    if not isinstance(graph, dict):
        return []
    nodes = sorted(list(graph))
    for node, dependencies in graph.items():
        if not isinstance(node, str) or not node or not isinstance(dependencies, list):
            return []
        seen = []
        for dependency in dependencies:
            if (not isinstance(dependency, str) or not dependency
                    or dependency not in graph or dependency in seen):
                return []
            seen.append(dependency)
    result = []
    for unused in nodes:
        chosen = None
        for node in nodes:
            if node not in result and chosen is None:
                ready = True
                for dependency in graph[node]:
                    if dependency not in result:
                        ready = False
                if ready:
                    chosen = node
        if chosen is None:
            return []
        result.append(chosen)
    return result
''',
        "public_test": """import unittest
from solution import dependency_order


class DependencyOrderTests(unittest.TestCase):
    def test_chain(self):
        graph = {"deploy": ["test"], "test": ["build"], "build": []}
        self.assertEqual(dependency_order(graph), ["build", "test", "deploy"])

    def test_parallel_nodes_are_lexical(self):
        self.assertEqual(dependency_order({"b": [], "a": [], "c": ["a"]}), ["a", "b", "c"])

    def test_cycle_returns_empty(self):
        self.assertEqual(dependency_order({"a": ["b"], "b": ["a"]}), [])


if __name__ == "__main__":
    unittest.main()
""",
    },
    "event_deduplication": {
        "title": "有序事件去重与冲突检测逻辑",
        "function_name": "dedupe_events",
        "parameters": ("events",),
        "spec": """# 事件去重规则

实现 `dedupe_events(events)`：

- events 是字典列表，每项必须且只能包含 `id`、`sequence`、`payload`。
- id 是非空字符串；sequence 是非负 int 且不能是 bool；payload 是 str、int 或 bool。
- 同一 id 的完全相同事件只保留一份；sequence 或 payload 不同即为冲突，整体返回空列表。
- 返回按 `(sequence, id)` 升序排列的新字典列表；任一输入无效也返回空列表。
- 不得修改输入及其中的字典。
""",
        "initial": '''def dedupe_events(events):
    """Keep the last event for every id."""
    seen = {}
    for event in events:
        seen[event["id"]] = event
    result = list(seen.items())
    result.sort(key=lambda item: (item[1]["sequence"], item[0]))
    output = []
    for event_id, event in result:
        output.append(event)
    return output
''',
        "reference": '''def dedupe_events(events):
    """Validate, deduplicate and order immutable event records."""
    if not isinstance(events, list):
        return []
    seen = {}
    result = []
    for event in events:
        if (not isinstance(event, dict) or len(event) != 3 or "id" not in event
                or "sequence" not in event or "payload" not in event):
            return []
        event_id = event["id"]
        sequence = event["sequence"]
        payload = event["payload"]
        if (not isinstance(event_id, str) or not event_id
                or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0
                or not isinstance(payload, (str, int, bool))):
            return []
        value = (sequence, payload)
        if event_id in seen and seen[event_id] != value:
            return []
        if event_id not in seen:
            seen[event_id] = value
            result.append({"id": event_id, "sequence": sequence, "payload": payload})
    result.sort(key=lambda item: (item["sequence"], item["id"]))
    return result
''',
        "public_test": """import unittest
from solution import dedupe_events


class DedupeEventsTests(unittest.TestCase):
    def test_deduplicates_and_sorts(self):
        events = [
            {"id": "b", "sequence": 2, "payload": "y"},
            {"id": "a", "sequence": 1, "payload": "x"},
            {"id": "a", "sequence": 1, "payload": "x"},
        ]
        expected = [
            {"id": "a", "sequence": 1, "payload": "x"},
            {"id": "b", "sequence": 2, "payload": "y"},
        ]
        self.assertEqual(dedupe_events(events), expected)

    def test_conflict_returns_empty(self):
        events = [
            {"id": "a", "sequence": 1, "payload": "x"},
            {"id": "a", "sequence": 2, "payload": "x"},
        ]
        self.assertEqual(dedupe_events(events), [])

    def test_input_is_not_modified(self):
        event = {"id": "a", "sequence": 1, "payload": True}
        self.assertEqual(dedupe_events([event]), [event])
        self.assertEqual(event, {"id": "a", "sequence": 1, "payload": True})


if __name__ == "__main__":
    unittest.main()
""",
    },
    "rate_limit_window": {
        "title": "滑动窗口限流判定逻辑",
        "function_name": "admit_requests",
        "parameters": ("timestamps", "limit", "window"),
        "spec": """# 滑动窗口限流规则

实现 `admit_requests(timestamps, limit, window)`：

- timestamps 是按非递减顺序排列的非负 int 列表；bool 不算整数。
- limit 和 window 都是正整数，且不能是 bool。
- 逐项返回 bool：仅统计此前已接纳且满足 `prior > current - window` 的请求。
- 被拒绝的请求不占用配额；边界 `prior == current - window` 已离开窗口。
- 任一输入无效时返回空列表，不得修改输入。
""",
        "initial": '''def admit_requests(timestamps, limit, window):
    """Apply a sliding-window request limit."""
    decisions = []
    for current in timestamps:
        count = 0
        for prior in timestamps:
            if prior < current and prior > current - window:
                count += 1
        decisions.append(count < limit)
    return decisions
''',
        "reference": '''def admit_requests(timestamps, limit, window):
    """Admit requests using only earlier requests that were accepted."""
    if (not isinstance(timestamps, list) or not isinstance(limit, int)
            or isinstance(limit, bool) or limit <= 0 or not isinstance(window, int)
            or isinstance(window, bool) or window <= 0):
        return []
    previous = None
    for timestamp in timestamps:
        if (not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0
                or previous is not None and timestamp < previous):
            return []
        previous = timestamp
    accepted = []
    decisions = []
    for current in timestamps:
        count = 0
        for prior in accepted:
            if prior > current - window:
                count += 1
        admitted = count < limit
        decisions.append(admitted)
        if admitted:
            accepted.append(current)
    return decisions
''',
        "public_test": """import unittest
from solution import admit_requests


class AdmitRequestsTests(unittest.TestCase):
    def test_rejected_request_does_not_consume_capacity(self):
        self.assertEqual(admit_requests([0, 1, 2, 10], 2, 10), [True, True, False, True])

    def test_boundary_has_left_window(self):
        self.assertEqual(admit_requests([0, 10], 1, 10), [True, True])

    def test_unsorted_input_is_invalid(self):
        self.assertEqual(admit_requests([2, 1], 1, 5), [])


if __name__ == "__main__":
    unittest.main()
""",
    },
    "config_overlay": {
        "title": "受保护配置覆盖逻辑",
        "function_name": "apply_overrides",
        "parameters": ("base", "overrides", "protected"),
        "spec": """# 配置覆盖规则

实现 `apply_overrides(base, overrides, protected)`：

- base/overrides 是两层字典：非空 section 和 key 字符串映射到 str、int 或 bool 原子值。
- overrides 只能修改 base 已存在的 section/key；protected 是 `(section, key)` 唯一二元组列表。
- base 本身不是字典，或其 section/key/value 不是上述合法两层结构时，返回 `(False, {})`。
- base 合法后，若修改受保护项、使用未知项、保护项重复或其余输入无效，返回
  `(False, base 的两层副本)`；成功返回 `(True, 覆盖后的两层新字典)`。
- 不得修改任一输入或共享内部 section 字典。
""",
        "initial": '''def apply_overrides(base, overrides, protected):
    """Overlay nested configuration values."""
    result = base.copy()
    for section, values in overrides.items():
        for key, value in values.items():
            result[section][key] = value
    return True, result
''',
        "reference": '''def apply_overrides(base, overrides, protected):
    """Atomically apply validated overrides while respecting protected keys."""
    if not isinstance(base, dict):
        return False, {}
    for section, values in base.items():
        if not isinstance(section, str) or not section or not isinstance(values, dict):
            return False, {}
        for key, value in values.items():
            if (not isinstance(key, str) or not key
                    or not isinstance(value, (str, int, bool))):
                return False, {}
    original = {}
    for section, values in base.items():
        section_copy = {}
        for key, value in values.items():
            section_copy[key] = value
        original[section] = section_copy
    if not isinstance(overrides, dict) or not isinstance(protected, list):
        return False, original
    protected_pairs = []
    for pair in protected:
        if (not isinstance(pair, tuple) or len(pair) != 2
                or not isinstance(pair[0], str) or not pair[0]
                or not isinstance(pair[1], str) or not pair[1] or pair in protected_pairs):
            return False, original
        protected_pairs.append(pair)
    result = {}
    for section, values in original.items():
        result[section] = values.copy()
    for section, values in overrides.items():
        if section not in original or not isinstance(values, dict):
            return False, original
        for key, value in values.items():
            if (key not in original[section] or (section, key) in protected_pairs
                    or not isinstance(value, (str, int, bool))):
                return False, original
            result[section][key] = value
    return True, result
''',
        "public_test": """import unittest
from solution import apply_overrides


class ApplyOverridesTests(unittest.TestCase):
    def test_known_override_does_not_mutate_base(self):
        base = {"app": {"debug": False, "name": "demo"}}
        expected = (True, {"app": {"debug": True, "name": "demo"}})
        self.assertEqual(apply_overrides(base, {"app": {"debug": True}}, []), expected)
        self.assertEqual(base, {"app": {"debug": False, "name": "demo"}})

    def test_protected_override_is_atomic(self):
        base = {"app": {"debug": False, "name": "demo"}}
        actual = apply_overrides(base, {"app": {"debug": True}}, [("app", "debug")])
        self.assertEqual(actual, (False, base))

    def test_unknown_key_fails(self):
        base = {"app": {"debug": False}}
        self.assertEqual(apply_overrides(base, {"app": {"port": 80}}, []), (False, base))


if __name__ == "__main__":
    unittest.main()
""",
    },
    "worker_assignment": {
        "title": "最小负载工作节点分配逻辑",
        "function_name": "assign_jobs",
        "parameters": ("jobs", "workers"),
        "spec": """# 工作节点分配规则

实现 `assign_jobs(jobs, workers)`：

- jobs 是 `(非空 job_id, 正整数 cost)` 列表，job_id 不得重复，bool 不算整数。
- workers 是非空且不重复的非空字符串列表。
- 按 jobs 原顺序逐个分配给当前累计 cost 最小的 worker；并列时按 workers 原顺序。
- 返回 `worker -> job_id 列表`，每个 worker 都必须出现。
- 任一无效输入返回空字典，不得修改输入。
""",
        "initial": '''def assign_jobs(jobs, workers):
    """Assign every job to the first worker."""
    result = {}
    for worker in workers:
        result[worker] = []
    for job_id, cost in jobs:
        result[workers[0]].append(job_id)
    return result
''',
        "reference": '''def assign_jobs(jobs, workers):
    """Assign jobs greedily to the least-loaded worker with stable ties."""
    if not isinstance(jobs, list) or not isinstance(workers, list) or not workers:
        return {}
    result = {}
    loads = {}
    for worker in workers:
        if not isinstance(worker, str) or not worker or worker in result:
            return {}
        result[worker] = []
        loads[worker] = 0
    seen_jobs = []
    for job in jobs:
        if not isinstance(job, tuple) or len(job) != 2:
            return {}
        job_id, cost = job
        if (not isinstance(job_id, str) or not job_id or job_id in seen_jobs
                or not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0):
            return {}
        seen_jobs.append(job_id)
        chosen = None
        for worker in workers:
            if chosen is None or loads[worker] < loads[chosen]:
                chosen = worker
        result[chosen].append(job_id)
        loads[chosen] += cost
    return result
''',
        "public_test": """import unittest
from solution import assign_jobs


class AssignJobsTests(unittest.TestCase):
    def test_balances_by_accumulated_cost(self):
        jobs = [("a", 5), ("b", 2), ("c", 2), ("d", 1)]
        self.assertEqual(assign_jobs(jobs, ["w1", "w2"]), {"w1": ["a"], "w2": ["b", "c", "d"]})

    def test_tie_uses_worker_order(self):
        actual = assign_jobs([("a", 1), ("b", 1), ("c", 1)], ["x", "y"])
        self.assertEqual(actual, {"x": ["a", "c"], "y": ["b"]})

    def test_duplicate_job_is_invalid(self):
        self.assertEqual(assign_jobs([("a", 1), ("a", 2)], ["x"]), {})


if __name__ == "__main__":
    unittest.main()
""",
    },
    "versioned_update": {
        "title": "乐观锁版本批量更新逻辑",
        "function_name": "apply_versioned_updates",
        "parameters": ("records", "updates"),
        "spec": """# 版本化批量更新规则

实现 `apply_versioned_updates(records, updates)`：

- records 是 `非空 id -> (非负版本 int, str/int/bool 值)` 字典，bool 不算版本。
- updates 是 `(id, expected_version, new_value)` 三元组列表；同一 id 在一批中不能重复。
- id 必须存在且当前版本等于 expected_version；成功后版本加一并替换值。
- 任一冲突或无效输入都原子失败，返回 `(False, 原 records 浅拷贝)`。
- records 本身不是字典时返回 `(False, {})`；成功返回 `(True, 新字典)`，不得修改输入。
""",
        "initial": '''def apply_versioned_updates(records, updates):
    """Apply optimistic-lock updates."""
    result = records.copy()
    for record_id, expected_version, value in updates:
        if record_id not in result or result[record_id][0] != expected_version:
            return False, result
        result[record_id] = (expected_version + 1, value)
    return True, result
''',
        "reference": '''def apply_versioned_updates(records, updates):
    """Atomically validate and apply optimistic-lock updates."""
    if not isinstance(records, dict):
        return False, {}
    original = records.copy()
    for record_id, record in records.items():
        if (not isinstance(record_id, str) or not record_id
                or not isinstance(record, tuple) or len(record) != 2
                or not isinstance(record[0], int) or isinstance(record[0], bool) or record[0] < 0
                or not isinstance(record[1], (str, int, bool))):
            return False, original
    if not isinstance(updates, list):
        return False, original
    result = original.copy()
    seen = []
    for update in updates:
        if not isinstance(update, tuple) or len(update) != 3:
            return False, original
        record_id, expected_version, value = update
        if (not isinstance(record_id, str) or not record_id or record_id in seen
                or not isinstance(expected_version, int) or isinstance(expected_version, bool)
                or expected_version < 0 or not isinstance(value, (str, int, bool))):
            return False, original
        if record_id not in result or result[record_id][0] != expected_version:
            return False, original
        seen.append(record_id)
        result[record_id] = (expected_version + 1, value)
    return True, result
''',
        "public_test": """import unittest
from solution import apply_versioned_updates


class VersionedUpdateTests(unittest.TestCase):
    def test_success_increments_versions(self):
        records = {"a": (1, "old"), "b": (0, False)}
        actual = apply_versioned_updates(records, [("a", 1, "new"), ("b", 0, True)])
        self.assertEqual(actual, (True, {"a": (2, "new"), "b": (1, True)}))
        self.assertEqual(records, {"a": (1, "old"), "b": (0, False)})

    def test_late_conflict_is_atomic(self):
        records = {"a": (1, "old"), "b": (2, "keep")}
        actual = apply_versioned_updates(records, [("a", 1, "new"), ("b", 1, "bad")])
        self.assertEqual(actual, (False, records))

    def test_duplicate_update_is_invalid(self):
        records = {"a": (0, "x")}
        updates = [("a", 0, "y"), ("a", 1, "z")]
        self.assertEqual(apply_versioned_updates(records, updates), (False, records))


if __name__ == "__main__":
    unittest.main()
""",
    },
}
