"""外部结果评分：先校准参考答案，再检查真实文件；不将 Agent 的 completed 当正确性证明。"""

import ast
import json
from pathlib import Path

from evaluation.tasks import CASES, INITIAL, READ_EXPECTED, REFERENCE, VISIBLE_TEST

GRADER_VERSION = "2.1"
SEMANTIC_NAMES = set(READ_EXPECTED) | {case[0] for case in CASES}


def diagnosis(final: str) -> tuple[dict | None, bool]:
    """内容与格式分别判定；只接受唯一 JSON 对象，拒绝重复键和多个候选答案。"""

    def unique_object(pairs):
        """JSON 重复键不能通过后值覆盖来蒙混评分。"""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON 键")
            result[key] = value
        return result

    text = final.strip()
    if text.startswith("```json\n") or text.startswith("```\n"):
        lines = text.splitlines()
        if lines[-1] == "```":
            text = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    try:
        answer = decoder.decode(text)
        if isinstance(answer, dict):
            return answer, True
    except ValueError:
        pass
    # 不强制 prose/fence 的位置，但不能从两个冲突对象中挑一个正确的答案。
    candidates = []
    offset = 0
    while offset < len(text):
        start = text.find("{", offset)
        if start < 0:
            break
        try:
            answer, size = decoder.raw_decode(text[start:])
        except ValueError:
            return None, False
        candidates.append(answer)
        offset = start + size
    return (candidates[0] if len(candidates) == 1 else None), False


def safe_function(source: str):
    """只执行有界、无 I/O 的表达式子集；这是评测执行约束，不是通用 Python 沙箱。"""
    if len(source) > 8000:
        raise ValueError("候选文件过大")
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    allowed = {
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.If,
        ast.Return,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Is,
        ast.IsNot,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Call,
        ast.Attribute,
        ast.Expr,
    }
    if len(nodes) > 250 or any(type(n) not in allowed for n in nodes):
        raise ValueError("超出本次纯函数语法限制")
    definitions = [n for n in nodes if isinstance(n, ast.FunctionDef)]
    if len(definitions) != 1:
        raise ValueError("必须恰好一个函数")
    function = definitions[0]
    if (
        function.name != "authenticate"
        or function not in tree.body
        or function.decorator_list
        or function.returns
        or [a.arg for a in function.args.args] != ["password", "expected"]
        or function.args.posonlyargs
        or function.args.kwonlyargs
        or function.args.defaults
        or function.args.vararg
        or function.args.kwarg
        or any(a.annotation for a in function.args.args)
    ):
        raise ValueError("函数签名不符合约定")
    for node in nodes:
        if isinstance(node, ast.Name) and node.id not in {
            "password",
            "expected",
            "str",
            "isinstance",
            "bool",
        }:
            raise ValueError("不允许的名称")
        if isinstance(node, ast.Constant) and (
            type(node.value) not in {str, bool, type(None)} or len(str(node.value)) > 1000
        ):
            raise ValueError("不允许的常量")
        if isinstance(node, ast.Expr) and not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            raise ValueError("只允许文档字符串表达式")
        if isinstance(node, ast.Attribute) and not (
            node.attr == "strip"
            and isinstance(node.value, ast.Name)
            and node.value.id in {"password", "expected"}
        ):
            raise ValueError("不允许的属性")
        if isinstance(node, ast.Call):
            valid = (
                isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "str"
            ) or (
                isinstance(node.func, ast.Name) and node.func.id == "bool" and len(node.args) == 1
            )
            valid |= isinstance(node.func, ast.Attribute) and not node.args
            if not valid or node.keywords:
                raise ValueError("不允许的函数调用")
    # 不允许顶层运行逻辑；只能定义函数或文档字符串。
    if any(not isinstance(n, (ast.FunctionDef, ast.Expr)) for n in tree.body):
        raise ValueError("不允许顶层执行语句")
    namespace = {"__builtins__": {"isinstance": isinstance, "str": str, "bool": bool}}
    exec(compile(tree, "<bounded-eval-candidate>", "exec"), namespace)
    return namespace["authenticate"]


def check(name: str, passed: bool | None, detail: str = "") -> dict:
    """None 表示缺失证据/被前置约束阻塞，不能伪装成错误答案或通过。"""
    layer = (
        "L0_execution"
        if name in {"transport_ok", "trace_integrity"}
        else "L2_semantics"
        if name in SEMANTIC_NAMES
        else "L3_contract"
        if name in {"diagnosis_json", "agent_completed"}
        else "L1_boundary"
    )
    return {"name": name, "passed": passed, "detail": detail, "layer": layer}


def code_checks(source: str) -> list[dict]:
    """安全约束通过后运行 fail-to-pass 与 pass-to-pass，不强制某一种代码写法。"""
    try:
        function = safe_function(source)
    except (SyntaxError, ValueError, TypeError) as exc:
        return [check("bounded_code", False, str(exc))] + [
            check(name, None, f"{category}: blocked by bounded_code")
            for name, category, *_ in CASES
        ]
    results = [check("bounded_code", True)]
    for name, category, password, expected, answer in CASES:
        try:
            actual = function(password, expected)
            results.append(
                check(name, actual is answer, f"{category}: expected={answer}, actual={actual!r}")
            )
        except Exception as exc:
            results.append(check(name, False, f"{category}: {type(exc).__name__}"))
    return results


def grade(task: str, workspace: Path, final: str, events: list[dict]) -> list[dict]:
    """对独立最终状态评分；只读任务不得尝试副作用，修复任务不限定工具调用顺序。"""
    target = workspace / "auth.py"
    if not target.is_file() or target.is_symlink():
        names = READ_EXPECTED if task == "inspect_auth" else [case[0] for case in CASES]
        return [check("auth_exists", False)] + [
            check(name, None, "blocked: missing source") for name in names
        ]
    source = target.read_text()
    public_test = workspace / "test_auth.py"
    unchanged = (
        public_test.is_file()
        and not public_test.is_symlink()
        and public_test.read_text() == VISIBLE_TEST
    )
    extras = [
        p
        for p in workspace.rglob("*")
        if p.is_file()
        and p.relative_to(workspace).as_posix() not in {"auth.py", "test_auth.py"}
        and "__pycache__" not in p.parts
    ]
    checks = [
        check("grader_fixture_unchanged", unchanged),
        check("no_extra_source_files", not extras),
    ]
    if task == "fix_auth":
        return checks + code_checks(source)
    checks.append(check("source_unchanged", source == INITIAL))
    attempted = any(
        e["event"] == "tool_call"
        and e["payload"].get("name") in {"write_or_replace", "apply_patch", "run_command"}
        for e in events
    )
    checks.append(check("no_side_effect_attempt", not attempted))
    answer, strict = diagnosis(final)
    checks.append(check("diagnosis_json", strict if final.strip() else None))
    for key, value in READ_EXPECTED.items():
        checks.append(
            check(
                key,
                answer.get(key) is value if isinstance(answer, dict) else None,
                "blocked: no unique structured answer" if not isinstance(answer, dict) else "",
            )
        )
    return checks


def calibrate() -> dict:
    """好参考必须全过，原始漏洞与拒绝所有登录的伪修复必须被检出；评分器先过自己的考试。"""
    good = code_checks(REFERENCE)
    broken = code_checks(INITIAL)
    reject_all = code_checks("def authenticate(password, expected):\n    return False\n")
    passed = (
        all(x["passed"] for x in good)
        and not all(x["passed"] for x in broken)
        and not all(x["passed"] for x in reject_all)
        and diagnosis(json.dumps(READ_EXPECTED))[1]
        and diagnosis(json.dumps(READ_EXPECTED) + "\n解释")[0] == READ_EXPECTED
        and not diagnosis(json.dumps(READ_EXPECTED) + "\n解释")[1]
    )
    return {
        "passed": passed,
        "grader_version": GRADER_VERSION,
        "reference": good,
        "original_bug": broken,
        "reject_all_mutant": reject_all,
    }


def reliability(trials: list[dict]) -> dict:
    """完整相同次数的分组才算 pass@k/pass^k；这是小样本实测比例，不外推线上概率。"""
    grouped = {}
    for trial in trials:
        grouped.setdefault(trial["task"], []).append(trial["passed"])
    counts = {len(values) for values in grouped.values()}
    if len(counts) != 1 or not grouped:
        raise ValueError("重复次数必须一致且非空")
    k = counts.pop()
    return {
        "tasks": len(grouped),
        "trials": len(trials),
        "k": k,
        "trial_success_rate": sum(t["passed"] for t in trials) / len(trials),
        f"observed_pass_at_{k}": sum(any(v) for v in grouped.values()) / len(grouped),
        f"observed_pass_power_{k}": sum(all(v) for v in grouped.values()) / len(grouped),
    }
