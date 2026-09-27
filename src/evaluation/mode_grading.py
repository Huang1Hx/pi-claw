"""三种 Agent 架构的统一外部评分器；只执行受限纯函数语法，不信任完成自述。"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

from evaluation.mode_tasks import TASKS

GRADER_VERSION = "agent-modes-1.2"
ALLOWED_CALLS = {"isinstance", "len", "sorted", "dict", "list", "tuple", "max", "min"}
ALLOWED_METHODS = {"copy", "get", "items", "append", "sort"}
EXPECTED_SIGNATURES = {
    "reserve": (("stock", "orders"), ()),
    "merge_windows": (("windows", "gap"), (0,)),
}


def check(name: str, passed: bool | None, layer: str, detail: str = "") -> dict:
    """输出带层次的独立检查；None 表示前置安全检查未通过而无法执行。"""
    return {"name": name, "passed": passed, "layer": layer, "detail": detail}


def safe_functions(source: str, expected_name: str) -> dict:
    """解析并执行有界纯函数子集；允许少量无递归辅助函数，但不是通用 OS 沙箱。"""
    if len(source) > 12_000:
        raise ValueError("候选文件过大")
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    allowed = {
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.If,
        ast.For,
        ast.Return,
        ast.Assign,
        ast.AugAssign,
        ast.Expr,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Is,
        ast.IsNot,
        ast.In,
        ast.NotIn,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Call,
        ast.Attribute,
        ast.Subscript,
        ast.Tuple,
        ast.List,
        ast.Dict,
        ast.Lambda,
        ast.keyword,
    }
    if len(nodes) > 500 or any(type(node) not in allowed for node in nodes):
        raise ValueError("超出受限纯函数语法")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    nested_functions = [node for node in nodes if isinstance(node, ast.FunctionDef)]
    function_names = {function.name for function in functions}
    if (
        not 1 <= len(functions) <= 4
        or len(nested_functions) != len(functions)
        or len(function_names) != len(functions)
        or expected_name not in function_names
    ):
        raise ValueError("必须定义指定函数，且最多包含三个顶层辅助函数")
    if any(
        not isinstance(node, ast.FunctionDef)
        and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        for node in tree.body
    ):
        raise ValueError("不允许顶层执行语句")

    graph = {name: set() for name in function_names}
    for function in functions:
        arguments = function.args
        if (
            function.decorator_list
            or function.returns
            or function.type_comment
            or arguments.vararg
            or arguments.kwarg
            or arguments.posonlyargs
            or arguments.kwonlyargs
            or arguments.kw_defaults
            or any(argument.annotation for argument in arguments.args)
        ):
            raise ValueError("不允许装饰器、注解或可变参数")
        names = tuple(argument.arg for argument in arguments.args)
        defaults = tuple(
            default.value if isinstance(default, ast.Constant) else object()
            for default in arguments.defaults
        )
        if function.name == expected_name:
            if (names, defaults) != EXPECTED_SIGNATURES[expected_name]:
                raise ValueError("函数签名与任务契约不一致")
        elif (
            function.name in ALLOWED_CALLS
            or function.name.startswith("__")
            or not 1 <= len(names) <= 4
            or defaults
        ):
            raise ValueError("辅助函数名称或签名不受支持")

        function_nodes = list(ast.walk(function))
        local_names = set(names)
        local_names.update(node.arg for node in function_nodes if isinstance(node, ast.arg))
        local_names.update(
            node.id
            for node in function_nodes
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        )
        for node in function_nodes:
            if isinstance(node, ast.Constant):
                if isinstance(node.value, int) and abs(node.value) > 1_000_000:
                    raise ValueError("整数常量超过限制")
                if isinstance(node.value, str) and len(node.value) > 2000:
                    raise ValueError("文本常量超过限制")
            if isinstance(node, ast.Attribute) and node.attr not in ALLOWED_METHODS:
                raise ValueError("不允许的属性访问")
            if isinstance(node, ast.Call):
                named = isinstance(node.func, ast.Name) and node.func.id in (
                    ALLOWED_CALLS | function_names
                )
                method = isinstance(node.func, ast.Attribute) and node.func.attr in ALLOWED_METHODS
                keywords_ok = not node.keywords
                is_sort = (isinstance(node.func, ast.Name) and node.func.id == "sorted") or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "sort"
                )
                if is_sort and len(node.keywords) == 1:
                    keyword = node.keywords[0]
                    value = keyword.value
                    lambda_args = value.args if isinstance(value, ast.Lambda) else None
                    keywords_ok = bool(
                        keyword.arg == "key"
                        and lambda_args is not None
                        and len(lambda_args.args) == 1
                        and not lambda_args.posonlyargs
                        and not lambda_args.kwonlyargs
                        and lambda_args.vararg is None
                        and lambda_args.kwarg is None
                        and not lambda_args.defaults
                        and not lambda_args.kw_defaults
                    )
                if not (named or method) or not keywords_ok:
                    raise ValueError("不允许的函数调用")
                if isinstance(node.func, ast.Name) and node.func.id in function_names:
                    graph[function.name].add(node.func.id)
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id
                not in local_names | ALLOWED_CALLS | function_names | {"str", "int", "bool"}
            ):
                raise ValueError(f"不允许的名称：{node.id}")

    def has_cycle(name: str, visiting: set[str], visited: set[str]) -> bool:
        """辅助函数调用图不能递归，避免候选在评分进程中无限调用。"""
        if name in visiting:
            return True
        if name in visited:
            return False
        visiting.add(name)
        if any(has_cycle(child, visiting, visited) for child in graph[name]):
            return True
        visiting.remove(name)
        visited.add(name)
        return False

    visited: set[str] = set()
    if any(has_cycle(name, set(), visited) for name in function_names):
        raise ValueError("不允许递归或循环辅助函数调用")
    builtins = {
        "isinstance": isinstance,
        "len": len,
        "sorted": sorted,
        "dict": dict,
        "list": list,
        "tuple": tuple,
        "max": max,
        "min": min,
        "str": str,
        "int": int,
        "bool": bool,
    }
    namespace = {"__builtins__": builtins}
    exec(compile(tree, "<bounded-mode-eval>", "exec"), namespace)
    return namespace


def _behavior_cases(task_id: str):
    """隐藏用例同时覆盖新要求和原有行为保留。"""
    if task_id == "inventory_reservation":
        return [
            (
                "basic_success",
                "pass_to_pass",
                ({"a": 5, "b": 2}, [("a", 2)]),
                (True, {"a": 3, "b": 2}),
            ),
            ("empty_batch", "pass_to_pass", ({"a": 1}, []), (True, {"a": 1})),
            ("exact_depletion", "pass_to_pass", ({"a": 2}, [("a", 2)]), (True, {"a": 0})),
            (
                "unknown_is_atomic",
                "fail_to_pass",
                ({"a": 5}, [("a", 2), ("x", 1)]),
                (False, {"a": 5}),
            ),
            (
                "duplicate_is_atomic",
                "fail_to_pass",
                ({"a": 3}, [("a", 2), ("a", 2)]),
                (False, {"a": 3}),
            ),
            ("zero_quantity", "fail_to_pass", ({"a": 3}, [("a", 0)]), (False, {"a": 3})),
            ("bool_quantity", "fail_to_pass", ({"a": 3}, [("a", True)]), (False, {"a": 3})),
            ("invalid_stock_value", "fail_to_pass", ({"a": -1}, []), (False, {"a": -1})),
            ("invalid_order_shape", "fail_to_pass", ({"a": 3}, [("a", 1, 2)]), (False, {"a": 3})),
            ("orders_must_be_list", "fail_to_pass", ({"a": 3}, (("a", 1),)), (False, {"a": 3})),
            ("order_must_be_tuple", "fail_to_pass", ({"a": 3}, [["a", 1]]), (False, {"a": 3})),
            ("invalid_stock_key", "fail_to_pass", ({"": 3}, []), (False, {"": 3})),
        ]
    return [
        ("unsorted_overlap", "pass_to_pass", ([(5, 8), (1, 3), (2, 6)], 0), [(1, 8)]),
        ("separate_windows", "pass_to_pass", ([(1, 2), (5, 7)], 0), [(1, 2), (5, 7)]),
        ("touching_windows", "pass_to_pass", ([(1, 3), (3, 4)], 0), [(1, 4)]),
        ("gap_merge", "fail_to_pass", ([(1, 2), (4, 6)], 2), [(1, 6)]),
        ("gap_boundary_separate", "fail_to_pass", ([(1, 2), (5, 6)], 2), [(1, 2), (5, 6)]),
        ("invalid_reverse", "fail_to_pass", ([(3, 2)], 0), []),
        ("invalid_bool_endpoint", "fail_to_pass", ([(True, 2)], 0), []),
        ("invalid_gap", "fail_to_pass", ([(1, 2)], -1), []),
        ("bool_gap", "fail_to_pass", ([(1, 2)], True), []),
        ("invalid_shape", "fail_to_pass", ([(1, 2, 3)], 0), []),
        ("windows_must_be_list", "fail_to_pass", (((1, 2),), 0), []),
    ]


def behavior_checks(task_id: str, source: str) -> list[dict]:
    """先校验语法安全，再运行隐藏用例并检查输入未被修改。"""
    task = TASKS[task_id]
    cases = _behavior_cases(task_id)
    try:
        function = safe_functions(source, task.function_name)[task.function_name]
    except (SyntaxError, ValueError, TypeError) as exc:
        return [check("bounded_candidate", False, "L1_boundary", str(exc))] + [
            check(name, None, "L2_semantics", f"{category}: blocked by bounded_candidate")
            for name, category, *_ in cases
        ]
    results = [check("bounded_candidate", True, "L1_boundary")]
    for name, category, arguments, expected in cases:
        copied = deepcopy(arguments)
        try:
            actual = function(*arguments)
            passed = actual == expected and arguments == copied
            detail = (
                f"{category}: expected={expected!r}, actual={actual!r}, "
                f"input_unchanged={arguments == copied}"
            )
        except Exception as exc:
            passed = False
            detail = f"{category}: {type(exc).__name__}: {exc}"
        results.append(check(name, passed, "L2_semantics", detail))
    return results


def grade(task_id: str, workspace: Path, agent_status: str) -> list[dict]:
    """评分最终文件、固定夹具和 Agent 终态；不要求特定工具调用顺序。"""
    task = TASKS[task_id]
    target = workspace / "solution.py"
    fixed = all(
        (workspace / name).is_file()
        and not (workspace / name).is_symlink()
        and (workspace / name).read_text() == content
        for name, content in {"SPEC.md": task.spec, "test_target.py": task.public_test}.items()
    )
    allowed = {"solution.py", "SPEC.md", "test_target.py"}
    extras = [
        p
        for p in workspace.rglob("*")
        if p.is_file()
        and p.relative_to(workspace).as_posix() not in allowed
        and "__pycache__" not in p.parts
    ]
    checks = [
        check("agent_completed", agent_status == "completed", "L0_execution", agent_status),
        check("fixtures_unchanged", fixed, "L1_boundary"),
        check("no_extra_files", not extras, "L1_boundary", ", ".join(str(p) for p in extras)),
        check("solution_exists", target.is_file() and not target.is_symlink(), "L1_boundary"),
    ]
    if not target.is_file() or target.is_symlink():
        return checks + [
            check(name, None, "L2_semantics", "blocked: missing solution")
            for name, *_ in _behavior_cases(task_id)
        ]
    return checks + behavior_checks(task_id, target.read_text())


def calibrate() -> dict:
    """参考实现必须全过，初始缺陷必须被至少一个隐藏用例检出。"""
    rows = {}
    for task_id, task in TASKS.items():
        reference = behavior_checks(task_id, task.reference)
        initial = behavior_checks(task_id, task.initial)
        rows[task_id] = {
            "reference": reference,
            "initial": initial,
            "passed": all(c["passed"] is True for c in reference)
            and any(c["passed"] is False for c in initial),
        }
    return {
        "grader_version": GRADER_VERSION,
        "passed": all(r["passed"] for r in rows.values()),
        "tasks": rows,
    }
