"""方案 B：按仓库固定 Python/依赖的受限容器环境，供外部评测复现。

- 只为轻量、纯 Python 仓库服务；容器显式限内存/CPU/PID，跑完即销毁；
- 环境准备阶段联网装依赖，判定阶段 `--network none`，减少网络型 PASS_TO_PASS 噪声；
- 产物只落在传入的实例目录（`runs/swebench-verified/work/<id>`）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from external_eval.harness import Runner, _pytest_group, _run

# 基础镜像只有 python:3.12，与这些老仓库的语义（如 pkgutil.get_loader 已过期）不兼容，
# 因此本地一次性构建了带源码编译 Python 3.9 的镜像 pi-claw-swebench:py39，所有实例复用。
DEFAULT_IMAGE = "pi-claw-swebench:py39"
PYTHON = "/opt/python3.9/bin/python3.9"

# 容器基础镜像只有 python:3.12，若用最新的 pytest 会与这些老仓库的 conftest 冲突
# （例如 flask 2.3.0.dev0 用到了 pytest 8 才移除的 monkeypatch.notset）。
# 因此按仓库把 pytest 固定到当年兼容的 7.x；pytest-dev/pytest 仓库自带 pytest，
# 用 `pip install -e .` 安装即可，不额外拉取。
REPO_PYTEST = {
    "psf/requests": "pytest==7.4.4",
    "pytest-dev/pytest": None,
    "pylint-dev/pylint": "pytest==7.4.4",
    "sphinx-doc/sphinx": "pytest==7.4.4",
    "pallets/flask": "pytest==7.4.4",
}

# 少数仓库的老代码依赖当年版本的直接依赖（新版会移除内部属性等）。
REPO_EXTRA_PINS = {
    "pallets/flask": ("werkzeug==2.2.2",),
    "pylint-dev/pylint": ("py==1.11.0", "pytest-timeout==2.1.0"),
    # Sphinx 的文档扩展在新版会反要求 Sphinx>=5，这里固定到与 3.x/4.x 时代匹配的一组。
    "sphinx-doc/sphinx": (
        "sphinxcontrib-applehelp==1.0.2",
        "sphinxcontrib-devhelp==1.0.2",
        "sphinxcontrib-htmlhelp==2.0.0",
        "sphinxcontrib-jsmath==1.0.1",
        "sphinxcontrib-qthelp==1.0.3",
        "sphinxcontrib-serializinghtml==1.1.5",
        "docutils==0.17.1",
        "jinja2==3.0.3",
        "markupsafe==2.0.1",
        "alabaster==0.7.12",
        "babel==2.9.1",
        "imagesize==1.3.0",
        "snowballstemmer==2.2.0",
        "Pygments==2.11.2",
    ),
}

# 硬性资源上限，避免占用宿主内存/CPU/PID。
LIMITS = ("--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "512")
CONTAINER_WORK = "/work"
# 宿主与容器 UID 不同，git 会因 "dubious ownership" 拒绝工作，
# 导致 setuptools_scm 等取不到版本；显式声明工作目录为安全目录。
GIT_SAFE_ENV = (
    "-e",
    "GIT_CONFIG_COUNT=1",
    "-e",
    "GIT_CONFIG_KEY_0=safe.directory",
    "-e",
    "GIT_CONFIG_VALUE_0=/work/repo",
)


def image_for(instance: dict, *, image: str | None = None) -> str:
    """返回评测容器镜像；默认用本地 py39 镜像，可用本地已有镜像覆盖。"""
    return image or DEFAULT_IMAGE


def install_spec(instance: dict) -> str:
    """按仓库给出容器内的依赖安装命令（固定 pytest 版本）。"""
    pytest_spec = REPO_PYTEST.get(instance["repo"], "pytest==7.4.4")
    steps = [
        "rm -rf /work/venv",
        f"{PYTHON} -m venv /work/venv",
        "/work/venv/bin/pip install -q --upgrade pip",
    ]
    if pytest_spec:
        steps.append(f'/work/venv/bin/pip install -q "{pytest_spec}"')
    for pin in REPO_EXTRA_PINS.get(instance["repo"], ()):
        steps.append(f'/work/venv/bin/pip install -q "{pin}"')
    steps.append("/work/venv/bin/pip install -q -e .")
    return " && ".join(steps)


def docker_available(*, runner: Runner | None = None) -> bool:
    """探测 Docker 守护进程是否可用。"""
    result = _run(runner, ["docker", "info", "--format", "{{.ServerVersion}}"], Path.cwd())
    return result.returncode == 0


def docker_prepare(
    instance: dict,
    inst_dir: Path,
    *,
    image: str | None = None,
    runner: Runner | None = None,
    timeout: int = 1800,
) -> str:
    """在受限容器里为该实例建 venv 并安装依赖，返回容器内解释器路径。"""
    inst_dir = Path(inst_dir)
    repo_dir = inst_dir / "repo"
    marker = inst_dir / ".docker-prepare.json"
    image = image_for(instance, image=image)
    spec = install_spec(instance)
    fingerprint = {"image": image, "spec": spec}
    if marker.is_file() and (inst_dir / "venv" / "bin" / "python").is_file():
        try:
            if json.loads(marker.read_text()) == fingerprint:
                return f"{CONTAINER_WORK}/venv/bin/python"
        except (ValueError, OSError):
            pass
    argv = [
        "docker",
        "run",
        "--rm",
        *LIMITS,
        *GIT_SAFE_ENV,
        "-v",
        f"{inst_dir}:{CONTAINER_WORK}",
        "-w",
        f"{CONTAINER_WORK}/repo",
        image,
        "bash",
        "-lc",
        spec,
    ]
    result = _run(runner, argv, repo_dir, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"容器内安装依赖失败：{result.stderr.strip()[:500]}")
    marker.write_text(json.dumps(fingerprint))
    return f"{CONTAINER_WORK}/venv/bin/python"


def docker_tester(
    instance: dict,
    inst_dir: Path,
    *,
    image: str | None = None,
    runner: Runner | None = None,
    timeout: int = 1800,
) -> Callable[[list[str]], dict[str, bool]]:
    """返回在 `--network none` 受限容器里跑一组测试 id 的判定函数。"""
    inst_dir = Path(inst_dir)
    repo_dir = inst_dir / "repo"

    prefix = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        *LIMITS,
        *GIT_SAFE_ENV,
        "-v",
        f"{inst_dir}:{CONTAINER_WORK}",
        "-w",
        f"{CONTAINER_WORK}/repo",
        image_for(instance, image=image),
        f"{CONTAINER_WORK}/venv/bin/python",
    ]

    def run(test_ids: list[str]) -> dict[str, bool]:
        return _pytest_group(
            prefix,
            repo_dir,
            test_ids,
            runner=runner,
            timeout=timeout,
            junit_arg=f"{CONTAINER_WORK}/junit.xml",
        )

    return run
