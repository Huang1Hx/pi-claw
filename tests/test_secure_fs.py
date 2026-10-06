"""文件工具的 dir-fd、symlink 与审批版本竞态回归。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core.schemas import ToolCall
from sandbox.models import CommandExecution, CommandRequest
from security.policy import WorkspacePolicy
from tools import ToolExecutor


def _execute(executor: ToolExecutor, name: str, **arguments: object):
    """以固定调用 ID 执行文件工具，保持测试只关注安全结果。"""
    return executor.execute(ToolCall("secure-fs", name, arguments))


def test_read_rejects_intermediate_symlink_inside_workspace(tmp_path: Path) -> None:
    """中间 symlink 即使仍指向工作区内，也不能改变模型实际读取的路径。"""
    real = tmp_path / "real"
    real.mkdir()
    (real / "target.txt").write_text("SAFE\n", encoding="utf-8")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    executor = ToolExecutor(WorkspacePolicy(tmp_path))

    result = _execute(executor, "read_file", path="alias/target.txt")

    assert result.ok is False
    assert "符号链接" in (result.error or "")


@pytest.mark.parametrize(
    ("operation", "fail_on_nested_open"),
    [("read", 1), ("missing", 2), ("list", 1)],
)
def test_directory_fstat_failure_closes_new_child_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fail_on_nested_open: int,
) -> None:
    """目录 open 后 fstat 异常时，普通访问、missing 定位和 walker 都不能泄漏 fd。"""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "target.txt").write_text("safe\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )
    original_open = os.open
    original_fstat = os.fstat
    nested_opens = 0
    failed_descriptor: list[int] = []

    def capture_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal nested_opens
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "nested" and kwargs.get("dir_fd") is not None:
            nested_opens += 1
            if nested_opens == fail_on_nested_open:
                failed_descriptor.append(descriptor)
        return descriptor

    def fail_child_fstat(descriptor: int):
        if failed_descriptor and descriptor == failed_descriptor[0]:
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr("security.secure_fs.os.open", capture_open)
    monkeypatch.setattr("security.secure_fs.os.fstat", fail_child_fstat)

    if operation == "read":
        _execute(executor, "read_file", path="nested/target.txt")
    elif operation == "missing":
        executor.preview(
            ToolCall(
                "missing-fd",
                "write_or_replace",
                {"path": "nested/new/target.txt", "old": "", "new": "created\n"},
            )
        )
    else:
        _execute(executor, "list_files", path="nested")

    assert failed_descriptor
    with pytest.raises(OSError):
        original_fstat(failed_descriptor[0])


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_or_replace", {"old": "before", "new": "after"}),
        ("apply_patch", {"edits": [{"old": "before", "new": "after"}]}),
    ],
)
def test_write_approval_rejects_same_content_inode_swap(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    """审批期间用同内容新 inode 替换目标时，不能只凭正文 SHA256 放行。"""
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    original_inode = target.stat().st_ino

    def swap_then_approve(*_args: object) -> bool:
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("before\n", encoding="utf-8")
        os.replace(replacement, target)
        assert target.stat().st_ino != original_inode
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=swap_then_approve,
    )

    result = _execute(executor, tool_name, path="target.txt", **arguments)

    assert result.ok is False
    assert "审批后目标文件发生变化" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_or_replace", {"old": "common", "new": "changed"}),
        ("apply_patch", {"edits": [{"old": "common", "new": "changed"}]}),
    ],
)
def test_handler_first_read_must_match_approved_preview_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    """审批后复核与 handler 首次读取之间换入文件时，不能编辑未展示正文。"""
    target = tmp_path / "target.txt"
    target.write_text("common in APPROVED\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )
    original_read_full = executor.workspace.secure_fs.read_full
    swapped = False

    def swap_before_handler_read(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            replacement = tmp_path / "replacement.txt"
            replacement.write_text("common in UNAPPROVED\n", encoding="utf-8")
            os.replace(replacement, target)
        return original_read_full(*args, **kwargs)

    monkeypatch.setattr(executor.workspace.secure_fs, "read_full", swap_before_handler_read)

    result = _execute(executor, tool_name, path="target.txt", **arguments)

    assert result.ok is False
    assert "审批预览版本" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "common in UNAPPROVED\n"


def test_preview_sha_and_diff_share_one_stable_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """竞态发生时，审批头中的 SHA 必须对应 diff 实际展示的同一份正文。"""
    target = tmp_path / "target.txt"
    target.write_text("before A\n", encoding="utf-8")
    executor = ToolExecutor(WorkspacePolicy(tmp_path, allow_write=True))
    original_version = executor.workspace.secure_fs.version
    calls = 0

    def racing_version(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            version = original_version(*args, **kwargs)
            target.write_text("before B\n", encoding="utf-8")
            return version
        target.write_text("before C\n", encoding="utf-8")
        return original_version(*args, **kwargs)

    monkeypatch.setattr(executor.workspace.secure_fs, "version", racing_version)

    preview = executor.preview(
        ToolCall(
            "preview-race",
            "write_or_replace",
            {"path": "target.txt", "old": "before", "new": "after"},
        )
    )

    assert preview is not None
    expected = hashlib.sha256(b"before A\n").hexdigest()
    assert f"target-preimage-sha256: {expected}" in preview
    assert "-before A" in preview


def test_invalid_preview_sentinel_cannot_authorize_later_valid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未取得具体 SecureFileVersion 时，即使复核哨兵相等也不能进入 handler。"""
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    approvals = 0

    def approve(*_args: object) -> bool:
        nonlocal approvals
        approvals += 1
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=approve,
    )
    executor.workspace.secure_fs.supported = False
    original_write_version = executor._write_version

    def invalid_then_enable(call: ToolCall):
        result = original_write_version(call)
        executor.workspace.secure_fs.supported = True
        return result

    monkeypatch.setattr(executor, "_write_version", invalid_then_enable)

    result = _execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert result.ok is False
    assert "安全文件版本" in (result.error or "")
    assert approvals == 1
    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize(
    ("old", "new", "initial"),
    [
        ("", "created\n", None),
        ("before", "after", "before\n"),
    ],
)
def test_write_rejects_parent_replaced_by_internal_symlink_after_preview(
    tmp_path: Path,
    old: str,
    new: str,
    initial: str | None,
) -> None:
    """预览后替换父目录为 symlink 时，不能写入另一个工作区目录。"""
    approved_parent = tmp_path / "approved"
    redirected_parent = tmp_path / "redirected"
    approved_parent.mkdir()
    redirected_parent.mkdir()
    approved_target = approved_parent / "target.txt"
    redirected_target = redirected_parent / "target.txt"
    if initial is not None:
        approved_target.write_text(initial, encoding="utf-8")
        redirected_target.write_text(initial, encoding="utf-8")

    def swap_then_approve(*_args: object) -> bool:
        approved_parent.rename(tmp_path / "approved-original")
        approved_parent.symlink_to(redirected_parent, target_is_directory=True)
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=swap_then_approve,
    )

    result = _execute(
        executor,
        "write_or_replace",
        path="approved/target.txt",
        old=old,
        new=new,
    )

    assert result.ok is False
    assert any(word in (result.error or "") for word in ("发生变化", "符号链接"))
    if initial is None:
        assert not redirected_target.exists()
    else:
        assert redirected_target.read_text(encoding="utf-8") == initial
    original_target = tmp_path / "approved-original" / "target.txt"
    assert initial is None or original_target.read_text(encoding="utf-8") == initial


def test_create_approval_binds_missing_target_parent_inode(tmp_path: Path) -> None:
    """创建预览后把父目录换成另一普通目录时，missing 版本也必须失效。"""
    approved_parent = tmp_path / "approved"
    replacement_parent = tmp_path / "replacement"
    approved_parent.mkdir()
    replacement_parent.mkdir()

    def replace_parent_then_approve(*_args: object) -> bool:
        approved_parent.rename(tmp_path / "approved-original")
        replacement_parent.rename(approved_parent)
        return True

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=replace_parent_then_approve,
    )

    result = _execute(
        executor,
        "write_or_replace",
        path="approved/new.txt",
        old="",
        new="created\n",
    )

    assert result.ok is False
    assert "审批后目标文件发生变化" in (result.error or "")
    assert not (approved_parent / "new.txt").exists()
    assert not (tmp_path / "approved-original" / "new.txt").exists()


def test_create_missing_nested_parents_from_bound_ancestor(tmp_path: Path) -> None:
    """父目录尚不存在时，审批版本绑定根 inode 后仍可安全创建完整相对路径。"""
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )

    result = _execute(
        executor,
        "write_or_replace",
        path="new/nested/target.txt",
        old="",
        new="created\n",
    )

    assert result.ok is True
    assert (tmp_path / "new/nested/target.txt").read_text(encoding="utf-8") == "created\n"


def test_atomic_create_preserves_private_mode_compatibility(tmp_path: Path) -> None:
    """新文件继续保持旧 tempfile+link 行为的 0600 权限，不能默认扩大宿主可读范围。"""
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )

    result = _execute(
        executor,
        "write_or_replace",
        path="private.txt",
        old="",
        new="private\n",
    )

    assert result.ok is True
    assert (tmp_path / "private.txt").stat().st_mode & 0o777 == 0o600


def test_list_files_rejects_intermediate_symlink(tmp_path: Path) -> None:
    """枚举也必须逐段 no-follow，不能把内部 symlink 当成真实目录入口。"""
    real = tmp_path / "real"
    real.mkdir()
    (real / "target.txt").write_text("safe\n", encoding="utf-8")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    executor = ToolExecutor(WorkspacePolicy(tmp_path))

    result = _execute(executor, "list_files", path="alias")

    assert result.ok is False
    assert "符号链接" in (result.error or "")


def test_secure_discovery_skips_sensitive_files_at_any_depth(tmp_path: Path) -> None:
    """安全 walker 不能因绕过 Workspace.resolve 而重新暴露嵌套 dotenv。"""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".env.local").write_text("HOST_SECRET\n", encoding="utf-8")
    (nested / "public.txt").write_text("public\n", encoding="utf-8")
    executor = ToolExecutor(WorkspacePolicy(tmp_path))

    listed = _execute(executor, "list_files", path=".")
    searched = _execute(executor, "search_text", query="HOST_SECRET", path=".")

    assert listed.ok is True
    assert listed.output["files"] == ["nested/public.txt"]
    assert searched.ok is True
    assert searched.output["matches"] == []


@pytest.mark.parametrize("tool_name", ["list_files", "search_text"])
def test_file_discovery_fails_closed_without_secure_dir_fd(
    tmp_path: Path,
    tool_name: str,
) -> None:
    """能力不足不能用空结果伪装完整枚举或搜索。"""
    (tmp_path / "target.txt").write_text("needle\n", encoding="utf-8")
    executor = ToolExecutor(WorkspacePolicy(tmp_path))
    executor.workspace.secure_fs.supported = False
    arguments = {"path": "."}
    if tool_name == "search_text":
        arguments["query"] = "needle"

    result = _execute(executor, tool_name, **arguments)

    assert result.ok is False
    assert "fail closed" in (result.error or "")


def test_search_text_cannot_read_outside_symlink_swapped_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搜索在 stat 后遇到外部 symlink 换入时，不能读取或返回宿主内容。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("safe text\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST_SECRET\n", encoding="utf-8")
    original_read_text = Path.read_text

    def swap_before_path_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", swap_before_path_read)
    executor = ToolExecutor(WorkspacePolicy(workspace))

    result = _execute(executor, "search_text", query="HOST_SECRET", path=".")

    assert result.ok is True
    assert result.output["matches"] == []
    assert "HOST_SECRET" not in str(result.output)


def test_final_open_rejects_parent_swapped_to_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler 读取后再换父目录时，root fd 重开复核必须拒绝且外部文件不变。"""
    workspace = tmp_path / "workspace"
    parent = workspace / "package"
    parent.mkdir(parents=True)
    target = parent / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "target.txt"
    outside_target.write_text("outside\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(workspace, allow_write=True),
        approval_callback=lambda *_args: True,
    )

    def swap_parent(_relative: Path) -> None:
        parent.rename(workspace / "package-original")
        parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(executor.workspace.secure_fs, "_before_final_open", swap_parent)

    result = _execute(
        executor,
        "write_or_replace",
        path="package/target.txt",
        old="before",
        new="after",
    )

    assert result.ok is False
    assert "父目录" in (result.error or "")
    assert outside_target.read_text(encoding="utf-8") == "outside\n"
    assert (workspace / "package-original" / "target.txt").read_text() == "before\n"


def test_final_open_rejects_same_content_target_inode_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler 已读取后同内容换 inode，最终打开必须比较完整版本而非只比较正文。"""
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    original_inode = target.stat().st_ino
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )

    def swap_target(_relative: Path) -> None:
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("before\n", encoding="utf-8")
        os.replace(replacement, target)

    monkeypatch.setattr(executor.workspace.secure_fs, "_before_final_open", swap_target)

    result = _execute(
        executor,
        "apply_patch",
        path="target.txt",
        edits=[{"old": "before", "new": "after"}],
    )

    assert result.ok is False
    assert "最终打开" in (result.error or "")
    assert target.stat().st_ino != original_inode
    assert target.read_text(encoding="utf-8") == "before\n"


def test_final_open_keeps_recheck_within_write_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终打开前换入超大文件时，应在读取正文前按写入预算 fail closed。"""
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
        max_write_bytes=32,
    )

    def swap_target(_relative: Path) -> None:
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("x" * 64, encoding="utf-8")
        os.replace(replacement, target)

    monkeypatch.setattr(executor.workspace.secure_fs, "_before_final_open", swap_target)

    result = _execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert result.ok is False
    assert "超过处理上限 32 字节" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "x" * 64


def test_read_rejects_hard_link_to_outside_workspace(tmp_path: Path) -> None:
    """普通名称不能通过硬链接把工作区外文件伪装成可读输入。"""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("HOST_SECRET", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.link(outside, workspace / "innocuous.txt")
    executor = ToolExecutor(WorkspacePolicy(workspace))

    result = _execute(executor, "read_file", path="innocuous.txt")

    assert result.ok is False
    assert "硬链接" in (result.error or "")
    assert outside.read_text(encoding="utf-8") == "HOST_SECRET"


def test_unsupported_secure_dir_fd_fails_host_write_closed(tmp_path: Path) -> None:
    """平台缺少 dir-fd/O_NOFOLLOW 时，Host 文件写不能静默退回 Path。"""
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_write=True),
        approval_callback=lambda *_args: True,
    )
    executor.workspace.secure_fs.supported = False

    result = _execute(
        executor,
        "write_or_replace",
        path="target.txt",
        old="before",
        new="after",
    )

    assert result.ok is False
    assert "fail closed" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "before\n"


def test_unsupported_secure_dir_fd_does_not_disable_docker_command_path(tmp_path: Path) -> None:
    """Host 文件能力 fail closed 不应阻止已隔离的 Docker 命令 Runner。"""

    class CaptureDockerRunner:
        backend = "docker"

        def run(
            self,
            request: CommandRequest,
            *,
            control: object | None = None,
        ) -> CommandExecution:
            return CommandExecution(0, "ok", "", False, backend=self.backend)

    executor = ToolExecutor(
        WorkspacePolicy(tmp_path, allow_commands=True, command_profile="coding"),
        command_runner=CaptureDockerRunner(),
    )
    executor.workspace.secure_fs.supported = False

    result = executor.execute(
        ToolCall("docker-command", "run_command", {"argv": ["git", "status"]}),
        approved=True,
    )

    assert result.ok is True
    assert result.output["backend"] == "docker"
