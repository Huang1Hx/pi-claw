from __future__ import annotations

from pathlib import Path

import pytest

from pi_claw.workspace import Workspace, WorkspaceViolation


def test_workspace_rejects_missing_or_file_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        Workspace(tmp_path / "missing")
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="不是目录"):
        Workspace(file_path)


@pytest.mark.parametrize("path", ["../outside.txt", "nested/../../outside.txt"])
def test_workspace_rejects_lexical_traversal(workspace: Path, path: str) -> None:
    with pytest.raises(WorkspaceViolation, match="穿越"):
        Workspace(workspace).resolve(path, allow_missing=True)


def test_workspace_rejects_absolute_external_path(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(WorkspaceViolation, match="工作区之外"):
        Workspace(workspace).resolve(outside)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".git/config",
        "credentials.json",
        "secret.txt",
        "certificate.pem",
        "private.key",
    ],
)
def test_workspace_rejects_sensitive_paths(workspace: Path, name: str) -> None:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sensitive", encoding="utf-8")
    with pytest.raises(WorkspaceViolation, match="拒绝访问"):
        Workspace(workspace).resolve(name)


def test_workspace_rejects_symlink_file_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-link-target.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside)

    with pytest.raises(WorkspaceViolation, match="工作区之外"):
        Workspace(workspace).resolve("escape.txt")


def test_workspace_rejects_symlink_parent_escape_for_new_file(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside-directory"
    outside.mkdir(exist_ok=True)
    (workspace / "escape-dir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceViolation, match="工作区之外"):
        Workspace(workspace).resolve("escape-dir/new.txt", allow_missing=True)


def test_workspace_allows_internal_absolute_path(workspace: Path) -> None:
    target = (workspace / "alpha.txt").resolve()
    assert Workspace(workspace).resolve(target) == target
