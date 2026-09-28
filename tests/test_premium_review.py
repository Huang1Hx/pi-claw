"""付费高级审查只由宿主确定性规则触发，不额外调用分类模型。"""

from pathlib import Path

import pytest

from config import load_settings
from routing.premium_review import premium_review_reason


@pytest.mark.parametrize(
    ("policy", "task", "files", "rounds", "expected"),
    [
        ("off", "请进行 Kimi 审查", {"src/auth.py"}, 3, None),
        ("explicit", "请进行 Kimi 审查", {"src/app.py"}, 1, "用户明确要求 Kimi 高级审查"),
        ("explicit", "修改认证和权限", {"src/auth.py"}, 1, None),
        ("risk", "修改认证和权限", {"src/app.py"}, 1, "任务涉及高风险能力"),
        ("risk", "修复登录", {"src/security/policy.py"}, 1, "改动路径涉及高风险能力"),
        ("risk", "修复普通逻辑", {"src/app.py"}, 2, "常规审查经历多轮返工"),
        (
            "risk",
            "更新模块",
            {f"src/module_{index}.py" for index in range(5)},
            1,
            "改动文件数量达到高级审查阈值",
        ),
        ("risk", "修复普通逻辑", {"src/app.py"}, 1, None),
        ("risk", "修改认证和权限", set(), 3, None),
        ("explicit", "请进行 premium review", set(), 1, None),
    ],
)
def test_premium_review_policy_has_auditable_triggers(policy, task, files, rounds, expected):
    """错误分支会造成普通任务付费或高风险任务漏审；期望值独立写死。"""
    assert premium_review_reason(task, files, rounds, policy) == expected


def _write_premium_config(tmp_path: Path, workflow: str) -> Path:
    """生成完整的双连接配置，测试只观察加载行为。"""
    path = tmp_path / "premium.toml"
    path.write_text(
        """
[connection]
base_url = "https://school.invalid/v1"
api_key_env = "SCHOOL_KEY"

[models.normal]
model = "deepseek-flash"

[models.premium]
base_url_env = "MOONSHOT_URL"
api_key_env = "MOONSHOT_KEY"
model = "kimi-k3"
max_attempts = 1

[workflow]
enabled = true
planner = "normal"
coder = "normal"
reviewer = "normal"
"""
        + workflow,
        encoding="utf-8",
    )
    return path


def _load_premium(tmp_path: Path, workflow: str):
    return load_settings(
        config_path=_write_premium_config(tmp_path, workflow),
        environ={
            "SCHOOL_KEY": "fake-school-secret",
            "MOONSHOT_URL": "https://moonshot.invalid/v1",
            "MOONSHOT_KEY": "fake-moonshot-secret",
        },
        no_env=True,
    )


def test_premium_workflow_configuration_loads(tmp_path):
    """启用 risk 时必须解析独立高级审查 profile 和有界请求数。"""
    settings = _load_premium(
        tmp_path,
        'premium_reviewer = "premium"\npremium_review_policy = "risk"\n'
        "max_premium_review_requests = 4\n",
    )
    assert settings.workflow.premium_reviewer == "premium"
    assert settings.workflow.premium_review_policy == "risk"
    assert settings.workflow.max_premium_review_requests == 4


@pytest.mark.parametrize(
    "workflow",
    [
        'premium_reviewer = "premium"\npremium_review_policy = "sometimes"\n',
        'premium_reviewer = "premium"\npremium_review_policy = "risk"\n'
        "max_premium_review_requests = 0\n",
        'premium_reviewer = "premium"\npremium_review_policy = "risk"\n'
        "max_premium_review_requests = 13\n",
        'premium_reviewer = "premium"\npremium_review_policy = "risk"\n'
        "max_premium_review_requests = true\n",
        'premium_reviewer = "missing"\npremium_review_policy = "risk"\n',
        'premium_review_policy = "risk"\n',
    ],
)
def test_invalid_premium_workflow_configuration_is_rejected(tmp_path, workflow):
    """无效策略、预算或 profile 必须在任何模型请求前失败。"""
    with pytest.raises(ValueError):
        _load_premium(tmp_path, workflow)
