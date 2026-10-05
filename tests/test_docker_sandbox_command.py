"""Docker Runner 的固定 argv、镜像身份和清理契约。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config.models import SandboxSettings
from core.control import ExecutionCancelled
from core.schemas import ToolCall
from sandbox.docker import DockerCommandRunner
from sandbox.models import CommandExecution, CommandRequest
from sandbox.snapshot import SnapshotCleanupError, WorkspaceSnapshotBuilder
from security.policy import WorkspacePolicy
from security.workspace import Workspace
from tools import ToolExecutor

IMAGE_ID = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _prepared_runner(tmp_path: Path) -> tuple[DockerCommandRunner, CommandRequest, object]:
    workspace = tmp_path / "project"
    cwd = workspace / "tests"
    cwd.mkdir(parents=True)
    (workspace / "main.py").write_text("print('ok')\n")
    request = CommandRequest(
        ["python", "-c", "print('ok')", "--network=host", "--privileged"],
        cwd,
        7,
        workspace_root=workspace,
    )
    runner = DockerCommandRunner(SandboxSettings(backend="docker"))
    runner._docker_binary = "/usr/local/bin/docker"
    runner._image_id = IMAGE_ID
    return runner, request, WorkspaceSnapshotBuilder().build(Workspace(workspace))


def test_docker_run_argv_is_fixed_and_model_arguments_are_after_image(tmp_path: Path) -> None:
    """模型字符串只能成为容器命令参数，不能注入 Docker 网络、挂载或 capability。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    try:
        argv = runner.build_run_argv(request, snapshot, "pi-claw-test")
    finally:
        snapshot.cleanup()

    assert argv == [
        "/usr/local/bin/docker",
        "run",
        "--name=pi-claw-test",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--init",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--pids-limit=128",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=1.0",
        "--user=65532:65532",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=1777",
        "--tmpfs=/workspace:rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=0700",
        f"--mount=type=bind,src={snapshot.path},dst=/input,readonly",
        "--env=HOME=/tmp",
        "--env=PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONPATH=/workspace/src",
        "--env=UV_PROJECT_ENVIRONMENT=/opt/venv",
        "--env=UV_OFFLINE=1",
        "--env=UV_NO_SYNC=1",
        "--env=UV_NO_ENV_FILE=1",
        "--env=PI_CLAW_CWD_RELATIVE=tests",
        IMAGE_ID,
        "python",
        "-c",
        "print('ok')",
        "--network=host",
        "--privileged",
    ]
    image_index = argv.index(IMAGE_ID)
    assert argv[image_index + 1 :] == request.argv
    assert sum("dst=/input" in item for item in argv) == 1
    assert all(str(request.workspace_root) not in item for item in argv)


def test_docker_preflight_resolves_absolute_binary_and_image_id(
    tmp_path: Path, monkeypatch
) -> None:
    """tag 只在预检解析一次，实际运行固定为 daemon 返回的不可变 image ID。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=IMAGE_ID + "\n", stderr="")

    monkeypatch.setattr("sandbox.docker.shutil.which", lambda _name: "/opt/docker/bin/docker")
    monkeypatch.setattr("sandbox.docker.subprocess.run", fake_run)
    runner = DockerCommandRunner()
    runner.preflight()

    assert runner.image_id == IMAGE_ID
    assert calls[0][0] == [
        "/opt/docker/bin/docker",
        "image",
        "inspect",
        runner.settings.image,
        "--format",
        "{{.Id}}",
    ]
    assert calls[0][1]["timeout"] == 10


def test_docker_client_uses_one_trusted_environment_for_all_daemon_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """inspect/run/rm 必须绑定同一 Docker context，且该环境绝不进入容器。"""
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/pi-claw-test-docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless-test")
    monkeypatch.setenv("DOCKER_CONFIG", "/tmp/pi-claw-docker-config")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-docker-client-env")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=IMAGE_ID + "\n", stderr="")

    monkeypatch.setattr("sandbox.docker.shutil.which", lambda _name: "/opt/docker/bin/docker")
    monkeypatch.setattr("sandbox.docker.subprocess.run", fake_run)
    runner = DockerCommandRunner()
    runner.preflight()
    environment = calls[0][1]["env"]

    assert environment["DOCKER_HOST"] == "unix:///tmp/pi-claw-test-docker.sock"
    assert environment["DOCKER_CONTEXT"] == "rootless-test"
    assert environment["DOCKER_CONFIG"] == "/tmp/pi-claw-docker-config"
    assert "OPENAI_API_KEY" not in environment
    assert runner._host_runner._environment == environment
    runner._remove_container("pi-claw-test")
    assert calls[-2][1]["env"] == environment
    assert calls[-1][1]["env"] == environment

    (tmp_path / "main.py").write_text("pass\n")
    request = CommandRequest(["python", "main.py"], tmp_path, 5, workspace_root=tmp_path)
    with WorkspaceSnapshotBuilder().build(Workspace(tmp_path)) as snapshot:
        argv = runner.build_run_argv(request, snapshot, "pi-claw-test")
    assert not any(item.startswith("--env=DOCKER_") for item in argv)


def test_docker_client_absolutizes_relative_config_paths(tmp_path: Path, monkeypatch) -> None:
    """相对 Docker 配置路径在 Runner 创建时固定，不能随各子进程 cwd 漂移。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_CONFIG", "docker-config")
    monkeypatch.setenv("DOCKER_CERT_PATH", "certs")
    monkeypatch.setenv("HOME", "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "xdg")

    runner = DockerCommandRunner()

    assert runner._docker_environment["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
    assert runner._docker_environment["DOCKER_CERT_PATH"] == str(tmp_path / "certs")
    assert runner._docker_environment["HOME"] == str(tmp_path / "home")
    assert runner._docker_environment["XDG_CONFIG_HOME"] == str(tmp_path / "xdg")


def test_docker_detects_image_tag_drift(tmp_path: Path, monkeypatch) -> None:
    """预检后的 tag 若指向新镜像必须失败，不能静默运行不同供应链输入。"""
    runner = DockerCommandRunner()
    runner._docker_binary = "/usr/local/bin/docker"
    runner._image_id = IMAGE_ID
    changed = "sha256:" + "b" * 64
    monkeypatch.setattr(
        "sandbox.docker.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=changed + "\n", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="漂移"):
        runner.assert_image_unchanged()


def test_cleanup_requires_probe_to_explicitly_confirm_container_absence(monkeypatch) -> None:
    """rm 成功或自己的 no-such 文本，都不能掩盖 inspect 的 daemon/权限失败。"""
    runner = DockerCommandRunner()
    runner._docker_binary = "/usr/local/bin/docker"

    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="permission denied"),
        ]
    )
    monkeypatch.setattr("sandbox.docker.subprocess.run", lambda *args, **kwargs: next(responses))
    assert runner._remove_container("pi-claw-test") is True

    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="No such container"),
            subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable"),
        ]
    )
    assert runner._remove_container("pi-claw-test") is True

    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 1, stdout="", stderr="No such object: another-container"
            ),
        ]
    )
    assert runner._remove_container("pi-claw-test") is True

    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 1, stdout="", stderr="permission denied; No such container: pi-claw-test"
            ),
        ]
    )
    assert runner._remove_container("pi-claw-test") is True


def test_cleanup_accepts_only_probe_no_such_as_confirmation(monkeypatch) -> None:
    runner = DockerCommandRunner()
    runner._docker_binary = "/usr/local/bin/docker"
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable"),
            subprocess.CompletedProcess([], 1, stdout="", stderr="No such object: pi-claw-test"),
        ]
    )
    monkeypatch.setattr("sandbox.docker.subprocess.run", lambda *args, **kwargs: next(responses))

    assert runner._remove_container("pi-claw-test") is False


def test_docker_run_cleans_container_and_snapshot_and_returns_safe_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    """命令正常结束也必须显式删除容器和快照，Trace 元数据不得含宿主路径。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    removed = []
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_new_container_name", lambda: "pi-claw-test")
    monkeypatch.setattr(runner, "_remove_container", lambda name: removed.append(name) or False)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: CommandExecution(0, "ok\n", "", False),
    )

    result = runner.run(request)

    assert removed == ["pi-claw-test"]
    assert not snapshot.path.parent.exists()
    assert result.backend == "docker"
    assert result.metadata == {
        "image_id": IMAGE_ID,
        "network": "none",
        "mount_count": 1,
        "memory_mib": 512,
        "cpus": 1.0,
        "pids": 128,
        "snapshot_manifest_sha256": snapshot.manifest_sha256,
        "snapshot_file_count": 1,
        "snapshot_total_bytes": 12,
    }
    assert str(request.workspace_root) not in repr(result.metadata)


def test_docker_result_redacts_private_snapshot_paths(tmp_path: Path, monkeypatch) -> None:
    """daemon 挂载错误即使回显 src，也不能把宿主临时路径送入模型或 Trace。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    private_path = str(snapshot.path)
    private_parent = str(snapshot.path.parent)
    resolved_path = str(snapshot.path.resolve())
    resolved_parent = str(snapshot.path.parent.resolve())
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: False)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: CommandExecution(
            125,
            f"mount source={private_path}; resolved={resolved_path}\n",
            f"daemon inspected {private_parent}; resolved={resolved_parent}\n",
            False,
        ),
    )

    result = runner.run(request)

    assert private_path not in result.stdout + result.stderr
    assert private_parent not in result.stdout + result.stderr
    assert resolved_path not in result.stdout + result.stderr
    assert resolved_parent not in result.stdout + result.stderr
    assert "<sandbox-input>" in result.stdout
    assert "<sandbox-snapshot>" in result.stderr


def test_tool_result_hides_snapshot_path_from_runner_exception(tmp_path: Path, monkeypatch) -> None:
    """Popen/cwd 异常只保留安全摘要，私有快照路径不得进入 ToolResult 或 Trace。"""
    runner, _request, snapshot = _prepared_runner(tmp_path)
    private_paths = {str(snapshot.path.parent), str(snapshot.path.parent.resolve())}
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: False)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: (_ for _ in ()).throw(
            FileNotFoundError(f"missing cwd {snapshot.path.parent.resolve()}")
        ),
    )

    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=runner,
    ).execute(ToolCall("command", "run_command", {"argv": ["git", "status"]}), approved=True)

    assert result.ok is False
    assert "Docker 隔离命令执行失败" in (result.error or "")
    assert all(path not in (result.error or "") for path in private_paths)
    assert not snapshot.path.parent.exists()


def test_tool_result_hides_snapshot_builder_exception_path(tmp_path: Path, monkeypatch) -> None:
    """快照尚未返回时的初始化异常也只能暴露固定安全摘要。"""

    class FailingBuilder:
        def build(self, source):
            raise OSError("failed at /private/tmp/pi-claw-snapshot-private/input")

    runner = DockerCommandRunner(snapshot_builder=FailingBuilder())
    runner._docker_binary = "/usr/local/bin/docker"
    runner._image_id = IMAGE_ID
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=runner,
    ).execute(ToolCall("command", "run_command", {"argv": ["git", "status"]}), approved=True)

    assert result.ok is False
    assert "Docker 输入快照构建失败" in (result.error or "")
    assert "/private/tmp" not in repr(result.to_dict())


def test_snapshot_builder_cleanup_failure_is_structured_in_tool_result(
    tmp_path: Path, monkeypatch
) -> None:
    """快照构建失败且清理失败时，ToolResult 必须记录 cleanup_failed。"""

    class CleanupFailingBuilder:
        def build(self, source):
            raise SnapshotCleanupError("fixture private path must stay hidden")

    runner = DockerCommandRunner(snapshot_builder=CleanupFailingBuilder())
    runner._docker_binary = "/usr/local/bin/docker"
    runner._image_id = IMAGE_ID
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=runner,
    ).execute(ToolCall("command", "run_command", {"argv": ["git", "status"]}), approved=True)

    assert result.ok is False
    assert result.metadata["sandbox"] == {"backend": "docker", "cleanup_failed": True}
    assert "fixture private path" not in (result.error or "")


def test_docker_cli_runs_from_private_snapshot_parent_not_user_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """宿主 Docker CLI 无需进入用户项目目录，避免 cwd 错误回显宿主工作区。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    captured = {}
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: False)

    def capture(inner, control=None):
        captured["request"] = inner
        return CommandExecution(0, "", "", False)

    monkeypatch.setattr(runner._host_runner, "run", capture)
    runner.run(request)

    assert captured["request"].cwd == snapshot.path.parent
    assert captured["request"].cwd != request.cwd


def test_docker_snapshot_cleanup_failure_marks_execution_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """即使命令退出码为零，快照残留也必须设置 cleanup_failed。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: False)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: CommandExecution(0, "", "", False),
    )
    real_cleanup = snapshot.cleanup

    def fail_cleanup() -> None:
        raise SnapshotCleanupError("fixture")

    monkeypatch.setattr(snapshot, "cleanup", fail_cleanup)
    try:
        result = runner.run(request)
    finally:
        monkeypatch.setattr(snapshot, "cleanup", real_cleanup)
        snapshot.cleanup()

    assert result.cleanup_failed is True


def test_docker_cancellation_still_cleans_and_propagates_cleanup_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """取消不能跳过容器/快照清理，残留状态要附着到共享停止异常。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: True)

    def cancelled(_request, *, control=None):
        raise ExecutionCancelled("cancelled")

    monkeypatch.setattr(runner._host_runner, "run", cancelled)

    with pytest.raises(ExecutionCancelled) as captured:
        runner.run(request)

    assert captured.value.cleanup_failed is True
    assert not snapshot.path.parent.exists()


def test_docker_failure_does_not_hide_cleanup_failure(tmp_path: Path, monkeypatch) -> None:
    """Docker CLI 本身失败时，后续清理失败仍须作为结构化事实向上传递。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(runner, "_remove_container", lambda _name: True)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: (_ for _ in ()).throw(OSError("docker unavailable")),
    )

    with pytest.raises(RuntimeError, match="清理失败") as captured:
        runner.run(request)

    assert getattr(captured.value, "cleanup_failed", False) is True
    assert getattr(captured.value, "backend", None) == "docker"
    assert not snapshot.path.parent.exists()


def test_snapshot_is_cleaned_when_container_name_generation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """快照建立后的任何异常都必须经过同一清理出口。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(
        runner,
        "_new_container_name",
        lambda: (_ for _ in ()).throw(RuntimeError("name generation failed")),
    )

    with pytest.raises(RuntimeError, match="Docker 隔离命令执行失败") as captured:
        runner.run(request)

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert not snapshot.path.parent.exists()


def test_snapshot_cleanup_survives_interrupt_during_container_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    """容器清理收到二次 Ctrl+C 时仍须尝试清理快照，再向上传递中断。"""
    runner, request, snapshot = _prepared_runner(tmp_path)
    monkeypatch.setattr(runner, "prepare_snapshot", lambda _request: snapshot)
    monkeypatch.setattr(runner, "assert_image_unchanged", lambda: None)
    monkeypatch.setattr(
        runner._host_runner,
        "run",
        lambda inner, control=None: CommandExecution(0, "", "", False),
    )
    monkeypatch.setattr(
        runner,
        "_remove_container",
        lambda _name: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(request)

    assert not snapshot.path.parent.exists()


def test_tool_result_exposes_sandbox_metadata_and_fails_on_cleanup_error(tmp_path: Path) -> None:
    """安全参数进入 Trace；清理失败时零退出码也不能被判定为成功。"""

    class CleanupFailureRunner:
        backend = "docker"

        def run(self, request, *, control=None):
            return CommandExecution(
                0,
                "",
                "",
                False,
                cleanup_failed=True,
                backend="docker",
                metadata={"image_id": IMAGE_ID, "network": "none", "mount_count": 1},
            )

    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=CleanupFailureRunner(),
    ).execute(ToolCall("command", "run_command", {"argv": ["git", "status"]}), approved=True)

    assert result.ok is False
    assert "清理失败" in (result.error or "")
    assert result.output["sandbox"] == {
        "image_id": IMAGE_ID,
        "network": "none",
        "mount_count": 1,
    }


def test_successful_docker_tool_result_uses_relative_policy_cwd(tmp_path: Path) -> None:
    """成功 Trace 的 policy metadata 也不能包含宿主工作区绝对路径。"""

    class SuccessfulDockerRunner:
        backend = "docker"

        def run(self, request, *, control=None):
            return CommandExecution(
                0,
                "ok\n",
                "",
                False,
                backend="docker",
                metadata={"image_id": IMAGE_ID, "network": "none", "mount_count": 1},
            )

    nested = tmp_path / "package"
    nested.mkdir()
    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=SuccessfulDockerRunner(),
    ).execute(
        ToolCall(
            "command",
            "run_command",
            {"argv": ["git", "status"], "cwd": "package"},
        ),
        approved=True,
    )

    assert result.ok is True
    assert result.metadata["policy"]["metadata"]["cwd"] == "package"
    assert str(tmp_path.resolve()) not in repr(result.to_dict())


def test_tool_result_records_cleanup_failure_raised_by_runner(tmp_path: Path) -> None:
    """Runner 异常不能让 cleanup_failed 从 ToolResult/Trace 元数据中消失。"""

    class RaisedCleanupFailure(RuntimeError):
        cleanup_failed = True
        backend = "docker"

    class FailingRunner:
        backend = "docker"

        def run(self, request, *, control=None):
            raise RaisedCleanupFailure("Docker 命令失败且沙箱清理失败")

    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=FailingRunner(),
    ).execute(ToolCall("command", "run_command", {"argv": ["git", "status"]}), approved=True)

    assert result.ok is False
    assert "清理失败" in (result.error or "")
    assert result.metadata["sandbox"] == {"backend": "docker", "cleanup_failed": True}


def test_sandbox_image_inputs_are_pinned_and_non_root() -> None:
    """Dockerfile 固定基础镜像和 uv 摘要，运行阶段使用无特权 UID。"""
    dockerfile = (ROOT / "docker" / "sandbox" / "Dockerfile").read_text()
    assert "python:3.12-slim@sha256:" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.12.10@sha256:" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "PYTHONPATH=/workspace/src" in dockerfile
    assert "UV_OFFLINE=1" in dockerfile
    assert "UV_NO_SYNC=1" in dockerfile
    assert "UV_NO_ENV_FILE=1" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/pi-claw-sandbox-entrypoint"]' in dockerfile


def test_sandbox_entrypoint_and_build_script_preserve_boundaries() -> None:
    """受信入口只复制 `/input`，构建脚本必须输出 image ID 与 SPDX SBOM。"""
    entrypoint = (ROOT / "docker" / "sandbox" / "entrypoint.sh").read_text()
    build = (ROOT / "scripts" / "build-sandbox-image.sh").read_text()
    assert "cp -R /input/. /workspace/" in entrypoint
    assert "chmod -R u+rwX /workspace" in entrypoint
    assert 'cd "/workspace/${PI_CLAW_CWD_RELATIVE}"' in entrypoint
    assert 'exec "$@"' in entrypoint
    assert "docker build" in build
    assert "docker image inspect" in build
    assert "docker scout sbom" in build
    assert "--format spdx" in build
