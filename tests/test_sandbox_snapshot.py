"""Docker 输入快照的敏感文件、特殊节点和资源预算边界。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from core.schemas import ToolCall
from sandbox.docker import DockerCommandRunner
from sandbox.models import CommandExecution, CommandRequest
from sandbox.snapshot import SnapshotCleanupError, SnapshotViolation, WorkspaceSnapshotBuilder
from security.policy import WorkspacePolicy
from security.workspace import Workspace
from tools import ToolExecutor


def test_snapshot_copies_regular_files_and_records_exclusions(tmp_path: Path) -> None:
    """载荷只含普通项目文件，manifest 在载荷外记录固定排除项。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('ok')\n")
    (source / "README.md").write_text("demo\n")
    excluded = {
        ".git": "version_control",
        ".env": "dotenv",
        ".env.example": "dotenv",
        ".venv": "virtual_environment",
        "runs": "runtime_artifacts",
        ".pi-claw": "session_data",
        "credentials.json": "credential",
        "server.pem": "credential",
        ".npmrc": "credential",
        ".pypirc": "credential",
        ".netrc": "credential",
        ".git-credentials": "credential",
        ".docker": "credential",
        ".kube": "credential",
        ".terraform": "credential",
        "terraform.tfstate": "credential",
        "prod.tfstate": "credential",
        "production.tfvars": "credential",
        "production.auto.tfvars.json": "credential",
        ".azure": "credential",
        ".gnupg": "credential",
        ".vault-token": "credential",
        "auth.json": "credential",
        ".credentials": "credential",
    }
    for relative in excluded:
        target = source / relative
        if relative not in {
            ".git",
            ".venv",
            "runs",
            ".pi-claw",
            ".docker",
            ".kube",
            ".terraform",
            ".azure",
            ".gnupg",
        }:
            target.write_text("must-not-copy\n")
        else:
            target.mkdir()
            (target / "private.txt").write_text("must-not-copy\n")

    with WorkspaceSnapshotBuilder().build(Workspace(source)) as snapshot:
        assert (snapshot.path / "src" / "main.py").read_text() == "print('ok')\n"
        assert (snapshot.path / "README.md").read_text() == "demo\n"
        assert not any((snapshot.path / relative).exists() for relative in excluded)
        assert snapshot.manifest_path.parent == snapshot.path.parent
        assert snapshot.manifest_path.parent != snapshot.path
        raw_manifest = snapshot.manifest_path.read_bytes()
        manifest = json.loads(raw_manifest)
        assert snapshot.manifest_sha256 == hashlib.sha256(raw_manifest).hexdigest()
        assert snapshot.file_count == 2
        assert snapshot.total_bytes == len(b"print('ok')\n") + len(b"demo\n")
        assert manifest["summary"] == {
            "excluded_count": len(excluded),
            "file_count": snapshot.file_count,
            "node_count": len(excluded) + 3,
            "total_bytes": snapshot.total_bytes,
        }
        assert {item["path"]: item["reason"] for item in manifest["excluded"]} == excluded
        assert [item["path"] for item in manifest["files"]] == ["README.md", "src/main.py"]
        assert all(set(item) == {"path", "sha256", "size"} for item in manifest["files"])
        owned_root = snapshot.path.parent

    assert not owned_root.exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "device"])
def test_snapshot_rejects_special_nodes(tmp_path: Path, monkeypatch, kind: str) -> None:
    """特殊节点不能被跟随、复制或静默跳过，整个快照必须失败。"""
    source = tmp_path / "source"
    source.mkdir()
    target = source / "special"
    if kind == "symlink":
        target.symlink_to(tmp_path / "outside")
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text("device-placeholder")
        builder = WorkspaceSnapshotBuilder()
        real_lstat_at = builder._lstat_at

        def fake_lstat_at(directory_descriptor: int, name: str):
            result = real_lstat_at(directory_descriptor, name)
            if name == target.name:
                values = list(result)
                values[stat.ST_MODE] = stat.S_IFCHR | 0o600
                return os.stat_result(values)
            return result

        monkeypatch.setattr(builder, "_lstat_at", fake_lstat_at)

    with pytest.raises(SnapshotViolation, match="特殊文件|符号链接"):
        (builder if kind == "device" else WorkspaceSnapshotBuilder()).build(Workspace(source))


def test_failed_snapshot_restores_readonly_directory_permissions_before_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    """部分目录已设 0555 后再失败，也要保留原始违规原因并删除整棵临时树。"""
    source = tmp_path / "source"
    completed = source / "a-completed"
    completed.mkdir(parents=True)
    (completed / "main.py").write_text("pass\n")
    os.mkfifo(source / "z-special")
    owned_root = tmp_path / "controlled-snapshot"

    def controlled_mkdtemp(*, prefix: str) -> str:
        assert prefix == "pi-claw-snapshot-"
        owned_root.mkdir()
        return str(owned_root)

    monkeypatch.setattr("sandbox.snapshot.tempfile.mkdtemp", controlled_mkdtemp)

    with pytest.raises(SnapshotViolation, match="特殊文件"):
        WorkspaceSnapshotBuilder().build(Workspace(source))

    assert not owned_root.exists()


def test_snapshot_initialization_failure_cleans_owned_root(tmp_path: Path, monkeypatch) -> None:
    """mkdtemp 之后 payload 初始化失败也必须删除私有根，不能落在统一清理区之外。"""
    source = tmp_path / "source"
    source.mkdir()
    owned_root = tmp_path / "controlled-snapshot"

    def controlled_mkdtemp(*, prefix: str) -> str:
        assert prefix == "pi-claw-snapshot-"
        owned_root.mkdir()
        (owned_root / "input").write_text("blocks mkdir")
        return str(owned_root)

    monkeypatch.setattr("sandbox.snapshot.tempfile.mkdtemp", controlled_mkdtemp)

    with pytest.raises(FileExistsError):
        WorkspaceSnapshotBuilder().build(Workspace(source))

    assert not owned_root.exists()


def test_snapshot_rejects_hard_link_to_outside_file(tmp_path: Path) -> None:
    """普通文件的外部硬链接绕过名称规则时也必须失败，外部内容保持不变。"""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("HOST_SECRET")
    source = tmp_path / "source"
    source.mkdir()
    os.link(outside, source / "innocuous.txt")

    with pytest.raises(SnapshotViolation, match="硬链接"):
        WorkspaceSnapshotBuilder().build(Workspace(source))

    assert outside.read_text() == "HOST_SECRET"


def test_snapshot_exclusion_name_cannot_hide_special_node(tmp_path: Path) -> None:
    """攻击者不能用 `.env` 等排除名称让 FIFO 绕过特殊节点检查。"""
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / ".env")

    with pytest.raises(SnapshotViolation, match="特殊文件"):
        WorkspaceSnapshotBuilder().build(Workspace(source))


def test_snapshot_rejects_directory_replaced_by_symlink_before_descent(
    tmp_path: Path, monkeypatch
) -> None:
    """目录 lstat 后若被换成外部 symlink，快照不能跟随到工作区之外。"""
    source = tmp_path / "source"
    child = source / "package"
    child.mkdir(parents=True)
    (child / "safe.py").write_text("SAFE = True\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must-not-copy")
    builder = WorkspaceSnapshotBuilder()
    swapped = False

    def swap_directory(relative: Path, source_path: Path) -> None:
        nonlocal swapped
        if relative == Path("package") and not swapped:
            source_path.rename(source / "package-original")
            source_path.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(builder, "_before_descend", swap_directory, raising=False)

    with pytest.raises(SnapshotViolation, match="目录.*发生变化|安全打开"):
        builder.build(Workspace(source))


@pytest.mark.parametrize(
    ("builder", "files", "message"),
    [
        (WorkspaceSnapshotBuilder(max_files=1), {"a.txt": b"a", "b.txt": b"b"}, "文件数"),
        (WorkspaceSnapshotBuilder(max_total_bytes=3), {"large.txt": b"four"}, "总字节"),
    ],
)
def test_snapshot_rejects_resource_limit_overflow(
    tmp_path: Path,
    builder: WorkspaceSnapshotBuilder,
    files: dict[str, bytes],
    message: str,
) -> None:
    """超限必须让快照整体失败，不能返回被截断的可执行输入。"""
    source = tmp_path / "source"
    source.mkdir()
    for name, content in files.items():
        (source / name).write_bytes(content)

    with pytest.raises(SnapshotViolation, match=message):
        builder.build(Workspace(source))


def test_snapshot_rejects_total_node_limit_overflow(tmp_path: Path) -> None:
    """大量空目录和排除项也要计入节点预算，不能绕过 max_files 消耗 inode。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty-a").mkdir()
    (source / "empty-b").mkdir()

    with pytest.raises(SnapshotViolation, match="节点数"):
        WorkspaceSnapshotBuilder(max_nodes=1).build(Workspace(source))


def test_snapshot_rejects_file_changed_while_copying(tmp_path: Path, monkeypatch) -> None:
    """复制前后的 inode/大小/时间不一致时拒绝竞态快照。"""
    source = tmp_path / "source"
    source.mkdir()
    target = source / "changing.txt"
    target.write_text("before")
    original = WorkspaceSnapshotBuilder._copy_regular_file

    def mutate_after_copy(self, directory_descriptor, name, source_path, destination, initial):
        result = original(
            self,
            directory_descriptor,
            name,
            source_path,
            destination,
            initial,
        )
        source_path.write_text("changed-after-copy")
        return result

    monkeypatch.setattr(WorkspaceSnapshotBuilder, "_copy_regular_file", mutate_after_copy)

    with pytest.raises(SnapshotViolation, match="复制期间发生变化"):
        WorkspaceSnapshotBuilder().build(Workspace(source))


def test_snapshot_rejects_same_size_change_with_restored_mtime(tmp_path: Path, monkeypatch) -> None:
    """同尺寸覆写即使恢复 mtime，也要通过 ctime 变化识别为竞态。"""
    source = tmp_path / "source"
    source.mkdir()
    target = source / "changing.txt"
    target.write_text("before")
    original = WorkspaceSnapshotBuilder._copy_regular_file

    def mutate_and_restore_mtime(
        self, directory_descriptor, name, source_path, destination, initial
    ):
        result = original(self, directory_descriptor, name, source_path, destination, initial)
        time.sleep(0.01)
        source_path.write_text("alterd")
        os.utime(source_path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        return result

    monkeypatch.setattr(WorkspaceSnapshotBuilder, "_copy_regular_file", mutate_and_restore_mtime)

    with pytest.raises(SnapshotViolation, match="复制期间发生变化"):
        WorkspaceSnapshotBuilder().build(Workspace(source))


def test_snapshot_payload_permissions_are_readable_by_non_root(tmp_path: Path) -> None:
    """bind mount 根和子目录可遍历，文件只读；可执行位只按源文件是否可执行保留。"""
    source = tmp_path / "source"
    package = source / "package"
    package.mkdir(parents=True)
    plain = package / "plain.py"
    executable = package / "tool.sh"
    plain.write_text("VALUE = 1\n")
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)

    with WorkspaceSnapshotBuilder().build(Workspace(source)) as snapshot:
        assert stat.S_IMODE(snapshot.path.stat().st_mode) == 0o555
        assert stat.S_IMODE((snapshot.path / "package").stat().st_mode) == 0o555
        assert stat.S_IMODE((snapshot.path / "package" / "plain.py").stat().st_mode) == 0o444
        assert stat.S_IMODE((snapshot.path / "package" / "tool.sh").stat().st_mode) == 0o555


def test_docker_prepares_fresh_snapshot_from_workspace_root(tmp_path: Path) -> None:
    """Docker 每次从完整工作区新建输入，不能把命令 cwd 当成工作区根。"""
    workspace = tmp_path / "project"
    cwd = workspace / "tests"
    cwd.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (cwd / "test_demo.py").write_text("def test_ok(): assert True\n")
    (workspace / ".env").write_text("TOKEN=must-not-copy\n")
    runner = DockerCommandRunner()
    request = CommandRequest(["python", "-m", "unittest"], cwd, 5, workspace_root=workspace)

    first = runner.prepare_snapshot(request)
    second = runner.prepare_snapshot(request)
    try:
        assert first.path != second.path
        assert (first.path / "pyproject.toml").is_file()
        assert (first.path / "tests" / "test_demo.py").is_file()
        assert not (first.path / ".env").exists()
    finally:
        first.cleanup()
        second.cleanup()


def test_command_tool_passes_approved_workspace_root_to_runner(tmp_path: Path) -> None:
    """Runner 取得受策略约束的根目录，不能从模型参数推测或扩大快照范围。"""

    class CaptureRunner:
        backend = "docker"
        request: CommandRequest | None = None

        def run(self, request: CommandRequest, *, control=None) -> CommandExecution:
            self.request = request
            return CommandExecution(0, "", "", False, backend=self.backend)

    nested = tmp_path / "package"
    nested.mkdir()
    runner = CaptureRunner()
    result = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=runner,
    ).execute(
        ToolCall("command", "run_command", {"argv": ["git", "status"], "cwd": "package"}),
        approved=True,
    )

    assert result.ok
    assert runner.request is not None
    assert runner.request.workspace_root == tmp_path.resolve()
    assert runner.request.cwd == nested.resolve()


def test_docker_snapshot_preparation_rejects_uv_sync(tmp_path: Path) -> None:
    """Docker 运行时依赖必须来自固定镜像，不能从工作区触发 uv sync。"""
    runner = DockerCommandRunner()
    request = CommandRequest(["uv", "sync", "--frozen"], tmp_path, 5, workspace_root=tmp_path)

    with pytest.raises(SnapshotViolation, match="依赖安装"):
        runner.prepare_snapshot(request)


def test_snapshot_cleanup_failure_is_explicit(tmp_path: Path, monkeypatch) -> None:
    """清理失败要抛出独立异常，供命令结果和 Trace 标记 cleanup_failed。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("pass\n")
    snapshot = WorkspaceSnapshotBuilder().build(Workspace(source))

    def fail_cleanup(_path: Path) -> None:
        raise OSError

    with monkeypatch.context() as patch:
        patch.setattr("sandbox.snapshot.shutil.rmtree", fail_cleanup)
        with pytest.raises(SnapshotCleanupError, match="清理失败"):
            snapshot.cleanup()

    snapshot.cleanup()


def test_snapshot_cleanup_does_not_swallow_child_disappearance(tmp_path: Path, monkeypatch) -> None:
    """只有清理开始前根已不存在才算幂等成功；中途 FileNotFound 必须 fail closed。"""
    source = tmp_path / "source"
    child = source / "package"
    child.mkdir(parents=True)
    (child / "main.py").write_text("pass\n")
    snapshot = WorkspaceSnapshotBuilder().build(Workspace(source))
    real_chmod = os.chmod

    def disappear_during_cleanup(path, mode, *, dir_fd=None, follow_symlinks=True):
        if Path(path).name == "package":
            raise FileNotFoundError(path)
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr("sandbox.snapshot.os.chmod", disappear_during_cleanup)
        with pytest.raises(SnapshotCleanupError, match="清理失败"):
            snapshot.cleanup()

    assert snapshot.path.parent.exists()
    snapshot.cleanup()
