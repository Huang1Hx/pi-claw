"""判断是否需要付费高级审查；只使用宿主证据，不调用额外分类模型。"""

import re
from collections.abc import Collection

_EXPLICIT = re.compile(r"kimi\s*(?:高级)?审查|高级审查|premium\s+review", re.I)
_RISK_TASK = re.compile(
    r"认证|鉴权|密钥|权限|命令执行|文件系统|会话持久化|并发|模型路由|安全策略|"
    r"\b(?:authentication|authorization|credential|api\s+key|permission|"
    r"command\s+execution|filesystem|session\s+persistence|concurrency|lock|"
    r"model\s+routing|security\s+policy)\b|锁",
    re.I,
)
_RISK_PATH = re.compile(
    r"(?:^|[/_.-])(?:auth|authentication|authorization|credential|secret|security|"
    r"permission|policy|shell|command|filesystem|session|persistence|concurrency|lock|"
    r"routing|config)(?:[/_.-]|$)",
    re.I,
)


def premium_review_reason(
    task: str,
    changed_files: Collection[str],
    review_rounds: int,
    policy: str,
) -> str | None:
    """返回第一条可审计触发原因；没有写后证据时绝不消费付费审查。"""
    if policy not in {"off", "explicit", "risk"}:
        raise ValueError("premium review policy 无效")
    if policy == "off" or not changed_files:
        return None
    if _EXPLICIT.search(task):
        return "用户明确要求 Kimi 高级审查"
    if policy == "explicit":
        return None
    if _RISK_TASK.search(task):
        return "任务涉及高风险能力"
    if any(_RISK_PATH.search(path) for path in changed_files):
        return "改动路径涉及高风险能力"
    if review_rounds >= 2:
        return "常规审查经历多轮返工"
    if len(changed_files) >= 5:
        return "改动文件数量达到高级审查阈值"
    return None
