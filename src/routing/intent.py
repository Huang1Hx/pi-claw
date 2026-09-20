"""保守的意图规则：仅短、明确的只读请求允许使用低成本模型。"""

import re

READ_TOOLS = frozenset({"list_files", "read_file", "search_text"})
_SIMPLE = re.compile(
    r"^(?:请)?(?:只读)?(?:列出|读取|查找|搜索|查看)(?:文件|目录|项目|README|readme|[\w./-]+)"
)
_ENGLISH = re.compile(r"^(?:please\s+)?(?:list|read|find|search|show)\b", re.I)
_COMPLEX = re.compile(
    r"修改|修复|编写|创建|重构|删除|实现|设计|优化|运行|执行|测试|安全|并发|架构|分析|总结|解释|以及|然后|并且|\band\b|\bthen\b|\b(write|edit|fix|run|execute|test|refactor|delete|implement|design|analyze|explain)\b",
    re.I,
)


def classify(task: str) -> tuple[bool, str]:
    """返回可否走低成本路由及可审计原因；不调用额外分类模型。"""
    if len(task) > 160 or "\n" in task or _COMPLEX.search(task):
        return False, "复杂、含副作用或长任务"
    if _SIMPLE.search(task.strip()) or _ENGLISH.search(task.strip()):
        return True, "明确的短只读任务"
    return False, "意图含糊，使用主力模型"
