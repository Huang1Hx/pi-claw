"""用本项目 Agent 在真实仓库上产补丁（外部评测专用，不改 src/）。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from config import load_settings
from core.agent import Agent
from core.context import ContextManager
from core.control import ExecutionControl
from routing import RoutedProvider
from security.policy import WorkspacePolicy
from tools import ToolExecutor

# 只读+写工作区、允许 git status/diff；命令一律自动批准（外部评测不启用 Docker 沙箱）。
AGENT_PROMPT = """你是 Pi-Claw，一个权限受控的本地 Coding Agent，正在真实开源仓库里修复一个 issue。

工作区路径：{root}

请严格按 Inspect -> Edit -> Report 执行：
1. 先读取相关源码与测试，确认失败原因。
2. 只修改实现代码，保持改动最小、通用；不要修改或新增测试文件。
3. 本环境不提供测试执行与联网安装；测试由外部评测运行，不要尝试运行或安装。
4. 完成后给出简短最终报告，说明改了哪些文件、为什么；给出最终报告即表示结束。
"""


class ExternalPolicy(WorkspacePolicy):
    """外部评测放宽策略：可写工作区、允许 git status/diff，自动审批。"""

    def __init__(self, root: str | Path) -> None:
        super().__init__(root, allow_write=True, allow_commands=True, command_profile="coding")


def _auto_approve(call, decision, preview=None) -> bool:
    """外部评测批量运行，自动批准策略允许的写入与命令。"""
    return True


def build_user_prompt(instance: dict) -> str:
    """只使用 problem_statement（不注入 hints，保持与官方 no-hint 设置一致）。"""
    return (
        f"仓库：{instance['repo']}\n"
        f"基线提交：{instance['base_commit']}\n\n"
        f"Issue 描述：\n{instance['problem_statement']}\n\n"
        "请修复该 issue，只改动实现代码。"
    )


def capture_diff(repo_dir: Path) -> str:
    """把工作区改动（含新增文件）导出为 unified diff；忽略 .gitignore 覆盖物。"""
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return diff


def run_agent(
    instance: dict,
    repo_dir: Path,
    *,
    config_path: Path,
    max_steps: int = 24,
    deadline_seconds: int = 1800,
    trace_path: Path | None = None,
) -> dict:
    """在干净 base_commit 上跑一个 Agent 回合并返回补丁与运行摘要。"""
    from external_eval.harness import reset_base

    reset_base(repo_dir, instance["base_commit"])
    settings = load_settings(config_path=Path(config_path).resolve(), cwd=Path(config_path).parent)
    executor = ToolExecutor(
        ExternalPolicy(repo_dir),
        approval_callback=_auto_approve,
        command_timeout=60,
        max_write_bytes=200_000,
    )
    provider = RoutedProvider(
        settings,
        mode=settings.workflow.coder,
        max_attempts=3,
        retry_base_delay=2.0,
        max_retry_delay=10.0,
    )
    agent = Agent(
        provider,
        executor,
        max_steps=max_steps,
        trace=trace_path,
        system_prompt=AGENT_PROMPT.format(root=repo_dir),
        context_manager=ContextManager(),
        completion_verifier=None,
    )
    control = ExecutionControl(deadline_monotonic=time.monotonic() + deadline_seconds)
    result = agent.run(build_user_prompt(instance), control=control)
    patch = capture_diff(repo_dir)
    return {
        "patch": patch,
        "status": result.status.value,
        "steps": result.steps,
        "usage": result.usage,
        "final_text": result.final_text,
    }
