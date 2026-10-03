"""命令执行后端契约与宿主兼容行为。"""

import sys

import pytest

from core.schemas import ToolCall
from sandbox.docker import DockerCommandRunner
from sandbox.host import HostCommandRunner
from sandbox.models import CommandRequest
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def test_host_runner_keeps_exit_and_output(tmp_path):
    result = HostCommandRunner(max_output_chars=20).run(
        CommandRequest([sys.executable, "-c", "print('x' * 40)"], tmp_path, 5)
    )
    assert result.returncode == 0
    assert result.stdout.startswith("x" * 20)
    assert result.truncated
    assert result.backend == "host"


def test_docker_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(RuntimeError, match="Docker"):
        DockerCommandRunner().preflight()


@pytest.mark.parametrize("argv", [[], "echo hi", ["echo", 2]])
def test_command_request_rejects_non_argv(argv, tmp_path):
    with pytest.raises((TypeError, ValueError)):
        CommandRequest(argv, tmp_path, 1)


def test_tool_executor_reports_host_backend(tmp_path):
    (tmp_path / "test_ok.py").write_text(
        "import unittest\nclass TestOK(unittest.TestCase):\n"
        "    def test_ok(self): self.assertTrue(True)\n"
    )
    result = ToolExecutor(WorkspacePolicy(tmp_path, allow_commands=True)).execute(
        ToolCall("id", "run_command", {"argv": [sys.executable, "-m", "unittest", "test_ok.py"]}),
        approved=True,
    )
    assert result.ok
    assert result.output["backend"] == "host"
