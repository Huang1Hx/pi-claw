"""用本项目 Agent 在真实仓库上产补丁（外部评测专用，不改 src/）。"""

from __future__ import annotations

import json
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


def _read_seed_patch(seed_patch) -> str:
    """`seed_patch` 既可是补丁文件路径，也可直接是补丁文本。"""
    if seed_patch is None:
        return ""
    if isinstance(seed_patch, (str, Path)):
        path = Path(seed_patch)
        if path.is_file():
            return path.read_text()
    return str(seed_patch)


def run_agent(
    instance: dict,
    repo_dir: Path,
    *,
    config_path: Path,
    max_steps: int = 24,
    deadline_seconds: int = 1800,
    trace_path: Path | None = None,
    checkpoint_path: Path | None = None,
    resume_from: Path | None = None,
    seed_patch: str | None = None,
    extra_instructions: str | None = None,
) -> dict:
    """在干净 base_commit 上跑一个 Agent 回合，返回补丁与运行摘要。

    续跑相关参数用于坏例自动修复：
    - `seed_patch`：先把上一轮的补丁恢复到工作区，回到"失误时的 checkpoint"；
    - `resume_from`：读取上一轮保存的对话历史，从同一条上下文继续；
    - `checkpoint_path`：本轮结束后把对话历史落盘，供下次续跑；
    - `extra_instructions`：把诊断出的失败原因作为纠正提示追加到任务里。
    """
    from external_eval.harness import apply_diff, reset_base

    reset_base(repo_dir, instance["base_commit"])
    seed_text = _read_seed_patch(seed_patch)
    if seed_text.strip():
        apply_diff(repo_dir, seed_text, label="checkpoint")
    history = None
    if resume_from and Path(resume_from).is_file():
        try:
            history = json.loads(Path(resume_from).read_text()).get("messages") or None
        except (ValueError, OSError):
            history = None
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
    task = build_user_prompt(instance)
    if extra_instructions:
        task = f"{task}\n\n补充要求：\n{extra_instructions}"
    result = agent.run(task, messages=history, control=control)
    patch = capture_diff(repo_dir)
    if checkpoint_path is not None:
        Path(checkpoint_path).write_text(
            json.dumps(
                {
                    "messages": agent.last_messages,
                    "steps": result.steps,
                    "status": result.status.value,
                },
                ensure_ascii=False,
            )
        )
    return {
        "patch": patch,
        "status": result.status.value,
        "steps": result.steps,
        "usage": result.usage,
        "final_text": result.final_text,
    }
