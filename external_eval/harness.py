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
    if not diff_text.strip():
        # 空补丁（如 Agent 未产出改动）按"无改动"处理，交由判定阶段判为未解决。
        return
    result = _run(runner, ["git", "apply", "--whitespace=nowarn", str(patch_file)], repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"应用 {label} 失败：{result.stderr.strip()[:400]}")


def parse_junit(xml_text: str, repo_dir: Path) -> dict[str, bool]:
    """把 pytest 的 junit xml 还原成 `nodeid -> 是否通过`；无法定位的用例忽略。"""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    results: dict[str, bool] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        parts = classname.split(".") if classname else []
        nodeid = None
        for k in range(len(parts), 0, -1):
            candidate = "/".join(parts[:k]) + ".py"
            if (repo_dir / candidate).is_file():
                nodeid = candidate + "::" + "::".join(parts[k:] + [name])
                break
        if nodeid is None:
            continue
        failed = any(child.tag in {"failure", "error"} for child in case)
        results[nodeid] = not failed
    return results


def _pytest_group(
    argv_prefix, repo_dir, test_ids, *, runner, timeout, junit_arg: str | None = None
) -> dict[str, bool]:
    """一次 pytest 调用跑完一组用例，用 junit xml 取每条结果。"""
    junit = repo_dir.parent / "junit.xml"
    if junit.exists():
        junit.unlink()
    # junit_arg 是"被执行进程视角"的路径（容器内为 /work/junit.xml，本地为宿主路径）。
    junit_arg = junit_arg or str(junit)
    # 直接传 nodeid 时，任一 id 在当前环境不存在都会让 pytest 以用法错误中止整组；
    # 因此改为按文件运行，再用 junit 逐条匹配（未匹配到的计为未通过）。
    files = list(dict.fromkeys(test_id.split("::", 1)[0] for test_id in test_ids))
    result = _run(
        runner,
        [
            *argv_prefix,
            "-m",
            "pytest",
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit_arg}",
            *files,
        ],
        repo_dir,
        timeout=timeout,
    )
    _ = result  # 返回码不直接用；判定以 junit 每条结果为准
    parsed = parse_junit(junit.read_text(), repo_dir) if junit.exists() else {}
    # 未出现在 junit 里的用例按"未通过"处理（收集/导入错误等）。
    return {test_id: parsed.get(test_id, False) for test_id in test_ids}


def local_tester(
    python: Path, repo_dir: Path, *, runner: Runner | None = None, timeout: int = 1200
):
    """返回"跑一组测试 id → {id: 是否通过}"的本地 venv 实现。"""

    def run(test_ids: list[str]) -> dict[str, bool]:
        return _pytest_group([str(python)], repo_dir, test_ids, runner=runner, timeout=timeout)

    return run


def evaluate_patch(
    instance: dict,
    repo_dir: Path,
    test_fn,
    prediction_patch: str,
    *,
    runner: Runner | None = None,
) -> dict:
    """按官方规则判定：全部 FAIL_TO_PASS 通过且全部 PASS_TO_PASS 仍通过。

    `test_fn(test_ids) -> {id: bool}` 由调用方决定执行环境（本地 venv 或受限额的容器）。
    """
    tests = instance_tests(instance)
    reset_base(repo_dir, instance["base_commit"], runner=runner)
    apply_diff(repo_dir, prediction_patch, runner=runner, label="prediction")
    apply_diff(repo_dir, instance["test_patch"], runner=runner, label="test_patch")

    fail_results = test_fn(tests["FAIL_TO_PASS"])
    pass_results = test_fn(tests["PASS_TO_PASS"])

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
