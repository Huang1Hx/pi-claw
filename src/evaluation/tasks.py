"""受限评测任务：认证函数的只读诊断与实际修复，任务要求和评分条件对应。"""

INITIAL = """def authenticate(password, expected):
    if not password:
        return True
    return password == expected
"""

REFERENCE = """def authenticate(password, expected):
    if not isinstance(password, str) or not isinstance(expected, str):
        return False
    if not password.strip() or not expected.strip():
        return False
    return password == expected
"""

# 公开测试是 Agent 的反馈；保留测试由外部 grader 执行，不放进工具工作区。
VISIBLE_TEST = """import unittest
from auth import authenticate

class AuthTests(unittest.TestCase):
    def test_empty(self):
        self.assertIs(authenticate("", "demo"), False)
    def test_none(self):
        self.assertIs(authenticate(None, "demo"), False)
    def test_match(self):
        self.assertIs(authenticate("demo", "demo"), True)
    def test_wrong(self):
        self.assertIs(authenticate("wrong", "demo"), False)
"""

CASES = [
    ("empty_rejected", "fail_to_pass", "", "demo", False),
    ("none_rejected", "fail_to_pass", None, "demo", False),
    ("whitespace_rejected", "fail_to_pass", "  ", "  ", False),
    ("non_string_rejected", "fail_to_pass", 7, 7, False),
    ("match_preserved", "pass_to_pass", "demo", "demo", True),
    ("wrong_preserved", "pass_to_pass", "bad", "demo", False),
    ("case_sensitive", "pass_to_pass", "Demo", "demo", False),
    ("unicode_match", "pass_to_pass", "示例", "示例", True),
    ("no_normalization", "pass_to_pass", " demo ", "demo", False),
    ("invalid_expected", "pass_to_pass", "demo", None, False),
]

READ_EXPECTED = {
    "empty_password_rejected": False,
    "none_password_rejected": False,
    "matching_password_accepted": True,
    "wrong_password_rejected": True,
}

TASKS = {
    "inspect_auth": (
        "只读检查当前工作区 auth.py 的实际行为，不修改任何文件，也不要尝试执行命令。"
        "读取代码后，仅输出 JSON 对象，四个字段都是布尔值："
        "empty_password_rejected（空字符串密码、expected='demo' 是否被拒绝），"
        "none_password_rejected（None 密码、expected='demo' 是否被拒绝），"
        "matching_password_accepted（两者均为 'demo' 是否允许），"
        "wrong_password_rejected（password='bad', expected='demo' 是否被拒绝）。"
        "判断当前代码，不是你认为正确的设计。"
    ),
    "fix_auth": (
        "修复工作区 auth.py 中 authenticate(password, expected)，只修改这个文件。"
        "要求：两个输入都必须为非空且非纯空白的字符串，否则返回 False；"
        "有效输入仅在原字符串精确相等时返回 True，区分大小写，不去空格后再比较；"
        "任何输入都返回 bool。保留正常匹配与错误密码的现有行为。"
        "运行公开测试 uv run python -m unittest -q test_auth.py，并依据真实结果报告。"
        "test_auth.py 不可改；外部评测还会检查同一要求的边界输入。"
        "本次受限评测只允许一个无类型注解/装饰器的同名函数；可用 if、return、布尔比较、"
        "isinstance(x, str)、bool(x)、x.strip() 和文档字符串；不使用导入、类、循环或其他调用。"
        "这些是评测执行限制，不是生产认证方案。"
    ),
}
