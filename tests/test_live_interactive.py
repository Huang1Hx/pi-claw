"""显式启用的真实模型交互冒烟：覆盖默认流式 chat 与普通斜杠命令。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.live_model
@pytest.mark.skipif(
    os.environ.get("PI_CLAW_LIVE_CHAT") != "1",
    reason="设置 PI_CLAW_LIVE_CHAT=1 才调用真实模型",
)
def test_real_model_default_chat_answers_question_without_repository_tools(tmp_path: Path) -> None:
    """沿 README 的单模型入口启动真实 CLI，检查普通问答、状态、用量和退出。"""

    project_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = tmp_path / "trace.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "interfaces",
            "chat",
            "--provider",
            "openai",
            "--workspace",
            str(workspace),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--trace",
            str(trace),
        ],
        cwd=project_root,
        input="2 加 3 等于多少？\n/status\n/usage\n/exit\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    # 不把模型正文、原始 Trace、URL 或凭据带入失败日志。
    assert completed.returncode == 0, "交互进程异常退出"
    assert trace.exists(), "交互进程未保存审计轨迹"
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    event_types = [event["event"] for event in events]
    assert "run_completed" in event_types, "普通问答没有完成"
    assert "provider_error" not in event_types, "真实模型请求失败"
    assert "tool_call" not in event_types, "普通问答触发了不必要的工作区工具"
    answer = next(
        event["payload"]["final_text"] for event in events if event["event"] == "run_completed"
    )
    assert "5" in answer or "五" in answer, "普通问答结果不正确"
    assert "已记录 Token" in completed.stdout, "/usage 未输出统计"
    assert f"工作区：{workspace}" in completed.stdout, "/status 未输出当前工作区"
    assert not list(workspace.iterdir()), "普通问答意外生成了工作区文件"
