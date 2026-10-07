"""Agent 与命令执行后端的停止、超时和清理语义。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.agent import Agent
from core.control import ExecutionCancelled, ExecutionControl, ExecutionDeadlineExceeded
from core.schemas import ModelReply, RunStatus, ToolCall
from providers import ScriptedProvider
from sandbox.host import HostCommandRunner
from sandbox.models import CommandRequest
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def test_execution_control_reports_remaining_time_and_deadline() -> None:
    """同一控制对象使用单调时钟计算余量，到期后拒绝开始新操作。"""
    now = [10.0]
    control = ExecutionControl(deadline_monotonic=15.0, clock=lambda: now[0])

    assert control.remaining_seconds() == pytest.approx(5.0)
    assert control.limit_timeout(9.0) == pytest.approx(5.0)

    now[0] = 15.0
    with pytest.raises(ExecutionDeadlineExceeded, match="截止时间"):
        control.checkpoint()


def test_execution_control_propagates_user_cancellation() -> None:
    """外层取消事件必须被所有共享该控制对象的参与者立即观察到。"""
    event = threading.Event()
    control = ExecutionControl(cancel_event=event)
    event.set()

    with pytest.raises(ExecutionCancelled, match="取消"):
        control.checkpoint()


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_execution_control_rejects_invalid_timeout(timeout: object) -> None:
    control = ExecutionControl()
    with pytest.raises((TypeError, ValueError)):
        control.limit_timeout(timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        (ExecutionControl(deadline_monotonic=0, clock=lambda: 1), RunStatus.DEADLINE),
        (
            ExecutionControl(cancel_event=(cancelled := threading.Event())),
            RunStatus.CANCELLED,
        ),
    ],
)
def test_agent_stops_before_provider_when_outer_control_is_closed(
    workspace: Path,
    control: ExecutionControl,
    expected: RunStatus,
) -> None:
    """外层预算关闭后，Agent 不能再产生一次模型请求。"""
    if expected is RunStatus.CANCELLED:
        cancelled.set()
    provider = ScriptedProvider([ModelReply.final("不应调用")])

    result = Agent(provider, ToolExecutor(WorkspacePolicy(workspace))).run("task", control=control)

    assert result.status is expected
    assert result.usage["requests"] == 0


def test_host_runner_bounds_both_output_streams_without_deadlock(tmp_path: Path) -> None:
    """stdout/stderr 即使持续输出也要同时排空，只保留固定上限。"""
    result = HostCommandRunner(max_output_chars=1024).run(
        CommandRequest(
            [
                "python3",
                "-c",
                "import sys; sys.stdout.write('o'*200000); sys.stderr.write('e'*200000)",
            ],
            tmp_path,
            5,
        )
    )

    assert result.returncode == 0
    assert result.truncated is True
    assert len(result.stdout) <= 1100
    assert len(result.stderr) <= 1100


def test_host_runner_reports_safe_latency_and_buffer_metrics(tmp_path: Path) -> None:
    """评测只需要数值型时延/缓冲指标，不能把命令或宿主路径写入元数据。"""
    result = HostCommandRunner(max_output_chars=128).run(
        CommandRequest([sys.executable, "-c", "print('ready')"], tmp_path, 2)
    )

    assert 0 <= result.metadata["first_output_seconds"] <= result.metadata["duration_seconds"]
    assert result.metadata["captured_output_bytes"] == len(b"ready\n")
    assert result.metadata["output_buffer_limit_bytes"] == 256
    assert all(str(tmp_path) not in repr(value) for value in result.metadata.values())


def test_independent_host_runners_execute_concurrently(tmp_path: Path) -> None:
    """并发命令拥有独立进程组和输出缓冲，结果不能串线。"""
    runner = HostCommandRunner(max_output_chars=1024)

    def execute(index: int):
        return runner.run(
            CommandRequest(
                [sys.executable, "-c", f"import time; time.sleep(.05); print('job-{index}')"],
                tmp_path,
                2,
            )
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, range(4)))

    assert [result.stdout.strip() for result in results] == [f"job-{i}" for i in range(4)]
    assert all(result.returncode == 0 for result in results)


def test_host_runner_escalates_to_kill_for_sigterm_ignoring_process(tmp_path: Path) -> None:
    """局部超时先发 TERM，宽限两秒后必须 KILL 整个进程组。"""
    pid_file = tmp_path / "pid.txt"
    started = time.monotonic()
    result = HostCommandRunner().run(
        CommandRequest(
            [
                "python3",
                "-c",
                (
                    "import os, pathlib, signal, time; "
                    f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(30)"
                ),
            ],
            tmp_path,
            0.2,
        )
    )

    assert pid_file.is_file()
    assert result.timed_out is True
    assert result.cleanup_failed is False
    assert 0 <= result.metadata["cleanup_seconds"] <= 3.5
    assert time.monotonic() - started < 3.5


def test_host_runner_kills_background_children_in_same_process_group(tmp_path: Path) -> None:
    """父进程退出也不能遗留忽略 TERM 的后台子进程。"""
    child_pid = tmp_path / "child-pid.txt"
    child_code = (
        "import os, pathlib, signal, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    result = HostCommandRunner().run(
        CommandRequest(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                    "time.sleep(30)"
                ),
            ],
            tmp_path,
            0.2,
        )
    )

    pid = int(child_pid.read_text())
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert result.timed_out is True
    assert result.cleanup_failed is False
    assert not state or state.startswith("Z")


def test_host_runner_cleans_background_child_after_parent_exits_normally(tmp_path: Path) -> None:
    """直属进程返回 0 也不能把继承管道的后台进程遗留在宿主机。"""
    child_pid = tmp_path / "normal-exit-child.txt"
    child_code = (
        "import os, pathlib, signal, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )

    result = HostCommandRunner().run(
        CommandRequest(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                    "time.sleep(.2)"
                ),
            ],
            tmp_path,
            5,
        )
    )

    pid = int(child_pid.read_text())
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert result.returncode == 0
    assert result.cleanup_failed is False
    assert not state or state.startswith("Z")


def test_host_runner_active_cancel_cleans_process_group(tmp_path: Path) -> None:
    """命令运行期间收到共享取消信号时，先清理进程再向 Agent 传播取消。"""
    started_file = tmp_path / "started.txt"
    control = ExecutionControl()
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            HostCommandRunner().run(
                CommandRequest(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib, time; "
                            f"pathlib.Path({str(started_file)!r}).write_text('ready'); "
                            "time.sleep(30)"
                        ),
                    ],
                    tmp_path,
                    10,
                ),
                control=control,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    for _ in range(100):
        if started_file.is_file():
            break
        time.sleep(0.01)
    control.cancel()
    thread.join(timeout=4)

    assert thread.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], ExecutionCancelled)


def test_host_runner_attaches_cleanup_failure_to_cancellation(tmp_path: Path) -> None:
    """取消后的残留进程/管道不能被原始取消异常掩盖。"""
    started_file = tmp_path / "cleanup-failure-started.txt"
    control = ExecutionControl()
    errors: list[BaseException] = []

    class CleanupFailureRunner(HostCommandRunner):
        @classmethod
        def _terminate_group(cls, process):
            HostCommandRunner._terminate_group(process)
            return True

    def execute() -> None:
        try:
            CleanupFailureRunner().run(
                CommandRequest(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib, time; "
                            f"pathlib.Path({str(started_file)!r}).write_text('ready'); "
                            "time.sleep(30)"
                        ),
                    ],
                    tmp_path,
                    10,
                ),
                control=control,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    for _ in range(100):
        if started_file.is_file():
            break
        time.sleep(0.01)
    control.cancel()
    thread.join(timeout=4)

    assert thread.is_alive() is False
    assert isinstance(errors[0], ExecutionCancelled)
    assert getattr(errors[0], "cleanup_failed", False) is True


def test_agent_trace_exposes_cleanup_failure_on_cancel(workspace: Path) -> None:
    """Runner 清理失败必须进入终态事件，不能仅显示普通 cancelled。"""
    (workspace / "test_alpha.py").write_text("import unittest\n")

    class CleanupFailureRunner:
        backend = "host"

        def run(self, request, *, control=None):
            error = ExecutionCancelled("测试取消")
            error.cleanup_failed = True
            raise error

    events = []
    result = Agent(
        ScriptedProvider(
            [
                ModelReply.call(
                    ToolCall(
                        "test",
                        "run_command",
                        {"argv": [sys.executable, "-m", "unittest", "test_alpha.py"]},
                    )
                )
            ]
        ),
        ToolExecutor(
            WorkspacePolicy(workspace, allow_commands=True, command_profile="coding"),
            approval_callback=lambda *_: True,
            command_runner=CleanupFailureRunner(),
        ),
        on_event=events.append,
    ).run("运行测试")

    stopped = next(event for event in events if event.type == "cancelled")
    assert result.status is RunStatus.CANCELLED
    assert "清理失败" in (result.error or "")
    assert stopped.data["cleanup_failed"] is True


def test_host_runner_attaches_cleanup_failure_to_keyboard_interrupt(tmp_path: Path) -> None:
    """直接 Ctrl+C 与事件取消必须使用同一清理失败可观测语义。"""

    class InterruptAfterSpawn(ExecutionControl):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        def checkpoint(self) -> None:
            self.checks += 1
            if self.checks >= 2:
                raise KeyboardInterrupt

    class CleanupFailureRunner(HostCommandRunner):
        @classmethod
        def _terminate_group(cls, process):
            HostCommandRunner._terminate_group(process)
            return True

    with pytest.raises(KeyboardInterrupt) as captured:
        CleanupFailureRunner().run(
            CommandRequest([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 10),
            control=InterruptAfterSpawn(),
        )

    assert getattr(captured.value, "cleanup_failed", False) is True


def test_agent_trace_exposes_cleanup_failure_on_keyboard_interrupt(workspace: Path) -> None:
    """Ctrl+C 清理失败要写入结果和 Trace，不能只显示普通用户中断。"""
    (workspace / "test_alpha.py").write_text("import unittest\n")

    class InterruptRunner:
        backend = "host"

        def run(self, request, *, control=None):
            error = KeyboardInterrupt()
            error.cleanup_failed = True
            raise error

    events = []
    result = Agent(
        ScriptedProvider(
            [
                ModelReply.call(
                    ToolCall(
                        "test",
                        "run_command",
                        {"argv": [sys.executable, "-m", "unittest", "test_alpha.py"]},
                    )
                )
            ]
        ),
        ToolExecutor(
            WorkspacePolicy(workspace, allow_commands=True, command_profile="coding"),
            approval_callback=lambda *_: True,
            command_runner=InterruptRunner(),
        ),
        on_event=events.append,
    ).run("运行测试")

    stopped = next(event for event in events if event.type == "cancelled")
    assert result.status is RunStatus.CANCELLED
    assert "清理失败" in (result.error or "")
    assert stopped.data["cleanup_failed"] is True


def test_interrupted_tool_is_persisted_and_not_replayed_on_resume(workspace: Path) -> None:
    """中断必须补齐 tool 观察；恢复会话只继续模型，不自动重放未知副作用。"""
    (workspace / "test_alpha.py").write_text("import unittest\n")

    class CancelRunner:
        backend = "host"

        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, *, control=None):
            self.calls += 1
            raise ExecutionCancelled("测试取消")

    runner = CancelRunner()
    history: list[dict] = []
    agent = Agent(
        ScriptedProvider(
            [
                ModelReply.call(
                    ToolCall(
                        "test",
                        "run_command",
                        {"argv": [sys.executable, "-m", "unittest", "test_alpha.py"]},
                    )
                ),
                ModelReply.final("已确认中断，未重放命令"),
            ]
        ),
        ToolExecutor(
            WorkspacePolicy(workspace, allow_commands=True, command_profile="coding"),
            approval_callback=lambda *_: True,
            command_runner=runner,
        ),
    )

    first = agent.run("运行测试", messages=history, control=ExecutionControl())
    interrupted = history[-1]
    second = agent.run("继续，但不要重放", messages=history, control=ExecutionControl())

    assert first.status is RunStatus.CANCELLED
    assert [message["role"] for message in history[:3]] == ["user", "assistant", "tool"]
    payload = __import__("json").loads(interrupted["content"])
    assert payload["metadata"] == {"interrupted": True, "replay_allowed": False}
    assert second.status is RunStatus.COMPLETED
    assert runner.calls == 1


def test_interrupted_multi_tool_reply_closes_every_announced_call(workspace: Path) -> None:
    """首个工具中断后，其余已声明调用也要补齐未执行结果，保持协议历史有效。"""
    (workspace / "test_alpha.py").write_text("import unittest\n")

    class CancelRunner:
        backend = "host"

        def run(self, request, *, control=None):
            raise ExecutionCancelled("测试取消")

    history: list[dict] = []
    result = Agent(
        ScriptedProvider(
            [
                ModelReply.calls(
                    [
                        ToolCall(
                            "a",
                            "run_command",
                            {"argv": [sys.executable, "-m", "unittest", "test_alpha.py"]},
                        ),
                        ToolCall("b", "read_file", {"path": "alpha.txt"}),
                    ]
                )
            ]
        ),
        ToolExecutor(
            WorkspacePolicy(workspace, allow_commands=True, command_profile="coding"),
            approval_callback=lambda *_: True,
            command_runner=CancelRunner(),
        ),
    ).run("运行并读取", messages=history)

    tool_messages = [message for message in history if message["role"] == "tool"]
    assert result.status is RunStatus.CANCELLED
    assert [message["tool_call_id"] for message in tool_messages] == ["a", "b"]
    second = __import__("json").loads(tool_messages[1]["content"])
    assert second["metadata"] == {
        "interrupted": True,
        "not_started": True,
        "replay_allowed": False,
    }
