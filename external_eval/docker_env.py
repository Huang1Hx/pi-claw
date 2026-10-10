"""方案 B：按仓库固定 Python/依赖的受限容器环境，供外部评测复现。

- 只为轻量、纯 Python 仓库服务；容器显式限内存/CPU/PID，跑完即销毁；
- 环境准备阶段联网装依赖，判定阶段 `--network none`，减少网络型 PASS_TO_PASS 噪声；
- 产物只落在传入的实例目录（`runs/swebench-verified/work/<id>`）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from external_eval.harness import Runner, _run

# SWE-bench 各仓库对应的 Python 版本（按仓库版本取经验值，用 gold 补丁自检校验）。
REPO_PYTHON = {
    "psf/requests": "3.9",
    "pytest-dev/pytest": "3.9",
    "pylint-dev/pylint": "3.9",
    "sphinx-doc/sphinx": "3.9",
    "pallets/flask": "3.8",
}
DEFAULT_PYTHON = "3.9"

# 硬性资源上限，避免占用宿主内存/CPU/PID。
LIMITS = ("--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "512")
CONTAINER_WORK = "/work"


def image_for(instance: dict) -> str:
    """按仓库选出固定 Python 的官方 slim 镜像。"""
    version = REPO_PYTHON.get(instance["repo"], DEFAULT_PYTHON)
    return f"python:{version}-slim"


def docker_available(*, runner: Runner | None = None) -> bool:
    """探测 Docker 守护进程是否可用。"""
    result = _run(runner, ["docker", "info", "--format", "{{.ServerVersion}}"], Path.cwd())
    return result.returncode == 0


def docker_prepare(
    instance: dict,
    inst_dir: Path,
    *,
    runner: Runner | None = None,
    timeout: int = 1800,
) -> str:
    """在受限容器里为该实例建 venv 并安装依赖，返回容器内解释器路径。"""
    inst_dir = Path(inst_dir)
    repo_dir = inst_dir / "repo"
    install = (
        "python -m venv /work/venv"
        " && /work/venv/bin/pip install -q --upgrade pip pytest"
        " && /work/venv/bin/pip install -q -e ."
    )
    argv = [
        "docker",
        "run",
        "--rm",
        *LIMITS,
        "-v",
        f"{inst_dir}:{CONTAINER_WORK}",
        "-w",
        f"{CONTAINER_WORK}/repo",
        image_for(instance),
        "bash",
        "-lc",
        install,
    ]
    result = _run(runner, argv, repo_dir, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"容器内安装依赖失败：{result.stderr.strip()[:500]}")
    return f"{CONTAINER_WORK}/venv/bin/python"


def docker_tester(
    instance: dict,
    inst_dir: Path,
    *,
    runner: Runner | None = None,
    timeout: int = 600,
) -> Callable[[str], bool]:
    """返回在 `--network none` 受限容器里跑单个测试 id 的判定函数。"""
    inst_dir = Path(inst_dir)
    repo_dir = inst_dir / "repo"

    def run(test_id: str) -> bool:
        argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            *LIMITS,
            "-v",
            f"{inst_dir}:{CONTAINER_WORK}",
            "-w",
            f"{CONTAINER_WORK}/repo",
            image_for(instance),
            f"{CONTAINER_WORK}/venv/bin/python",
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            test_id,
        ]
        return _run(runner, argv, repo_dir, timeout=timeout).returncode == 0

    return run
