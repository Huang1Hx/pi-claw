"""在隔离目录里准备仓库环境、应用补丁并按 SWE-bench 规则判定是否解决。"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from external_eval.swebench_verified import instance_tests

# runner(argv, cwd, stdin_text) -> 具备 returncode/stdout/stderr 的对象
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
DEFAULT_ROOT = Path("runs/swebench-verified/work")


def _run(
    runner: Runner | None,
    argv: list[str],
    cwd: Path,
    *,
    stdin_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """统一执行入口，便于在单元测试中注入假 runner。"""
    if runner is not None:
        return runner(argv, cwd, stdin_text)
    return subprocess.run(
        argv,
        cwd=str(cwd),
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def clone_base(instance: dict, root: Path = DEFAULT_ROOT, *, runner: Runner | None = None) -> Path:
    """克隆仓库并切到 base_commit；已存在则复用，保证可重入。"""
    root = Path(root)
    repo_dir = root / instance["instance_id"] / "repo"
    if (repo_dir / ".git").is_dir():
        return repo_dir
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{instance['repo']}.git"
    result = _run(runner, ["git", "clone", "--quiet", url, str(repo_dir)], repo_dir.parent)
    if result.returncode != 0:
        raise RuntimeError(f"克隆失败：{result.stderr.strip()[:400]}")
    checkout = _run(runner, ["git", "checkout", "--quiet", instance["base_commit"]], repo_dir)
    if checkout.returncode != 0:
        raise RuntimeError(f"切到 base_commit 失败：{checkout.stderr.strip()[:400]}")
    return repo_dir


def create_venv(
    repo_dir: Path,
    *,
    runner: Runner | None = None,
    python: str = "python3",
    python_version: str | None = None,
) -> Path:
    """创建仓库专用 venv 并安装包与 pytest；失败即报错，不静默降级。

    `python_version` 非空时用 `uv` 拉取并固定该 Python 版本，贴近年份较早的仓库。
    """
    venv = repo_dir.parent / "venv"
    py = venv / "bin" / "python"
    if not py.exists():
        create_argv = (
            ["uv", "venv", "--python", python_version, str(venv)]
            if python_version
            else [python, "-m", "venv", str(venv)]
        )
        result = _run(runner, create_argv, repo_dir.parent)
        if result.returncode != 0:
            raise RuntimeError(f"创建 venv 失败：{result.stderr.strip()[:400]}")
    if python_version:
        prefix = ["uv", "pip", "install", "--python", str(py), "--quiet"]
    else:
        prefix = [str(py), "-m", "pip", "install", "--quiet", "--upgrade"]
    install = _run(runner, [*prefix, "pytest"], repo_dir)
    if install.returncode != 0:
        raise RuntimeError(f"安装 pytest 失败：{install.stderr.strip()[:400]}")
    editable = _run(runner, [*prefix, "-e", "."], repo_dir)
    if editable.returncode != 0:
        raise RuntimeError(f"安装仓库自身失败：{editable.stderr.strip()[:400]}")
    return venv


def reset_base(repo_dir: Path, base_commit: str, *, runner: Runner | None = None) -> None:
    """回到干净的 base_commit，避免上一次 trial 的改动泄漏。"""
    for argv in (["git", "reset", "--hard", "--quiet", base_commit], ["git", "clean", "-fdq"]):
        result = _run(runner, argv, repo_dir)
        if result.returncode != 0:
            raise RuntimeError(f"重置失败：{result.stderr.strip()[:400]}")


def apply_diff(repo_dir: Path, diff_text: str, *, runner: Runner | None = None, label: str) -> None:
    """用 git apply 应用 unified diff；失败即报错并保留现场。"""
    patch_file = repo_dir.parent / f"{label}.diff"
    patch_file.write_text(diff_text)
    result = _run(runner, ["git", "apply", "--whitespace=nowarn", str(patch_file)], repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"应用 {label} 失败：{result.stderr.strip()[:400]}")


def _pytest(python: Path, repo_dir: Path, test_id: str, *, runner, timeout: int):
    return _run(
        runner,
        [str(python), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", test_id],
        repo_dir,
        timeout=timeout,
    )


def evaluate_patch(
    instance: dict,
    repo_dir: Path,
    python: Path,
    prediction_patch: str,
    *,
    runner: Runner | None = None,
    timeout: int = 600,
) -> dict:
    """按官方规则判定：全部 FAIL_TO_PASS 通过且全部 PASS_TO_PASS 仍通过。"""
    tests = instance_tests(instance)
    reset_base(repo_dir, instance["base_commit"], runner=runner)
    apply_diff(repo_dir, prediction_patch, runner=runner, label="prediction")
    apply_diff(repo_dir, instance["test_patch"], runner=runner, label="test_patch")

    fail_results = {}
    for test_id in tests["FAIL_TO_PASS"]:
        result = _pytest(python, repo_dir, test_id, runner=runner, timeout=timeout)
        fail_results[test_id] = result.returncode == 0

    pass_results = {}
    for test_id in tests["PASS_TO_PASS"]:
        result = _pytest(python, repo_dir, test_id, runner=runner, timeout=timeout)
        pass_results[test_id] = result.returncode == 0

    resolved = all(fail_results.values()) and all(pass_results.values())
    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "resolved": resolved,
        "fail_to_pass": fail_results,
        "pass_to_pass": pass_results,
        "evidence": f"ftp={len(fail_results)}/{len(tests['FAIL_TO_PASS'])} "
        f"ptp={sum(pass_results.values())}/{len(pass_results)}",
    }


def save_result(result: dict, out_dir: Path = Path("runs/swebench-verified/report")) -> Path:
    """把单实例结果写入隔离目录。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['instance_id']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return path


def clean_work(instance: dict, root: Path = DEFAULT_ROOT) -> None:
    """删除某实例的克隆与 venv，便于重跑。"""
    shutil.rmtree(Path(root) / instance["instance_id"], ignore_errors=True)
