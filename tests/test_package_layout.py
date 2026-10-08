"""防止目录重构后源码导入、打包清单和用户启动入口发生漂移。"""

import subprocess
import sys
import sysconfig
import tomllib
from importlib import import_module, metadata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "commands",
    "config",
    "core",
    "evaluation",
    "interfaces",
    "persistence",
    "providers",
    "routing",
    "sandbox",
    "security",
    "tools",
}


def test_source_layout_matches_wheel_package_list() -> None:
    """src 下直接放业务包，根目录保留项目文档、测试和运行产物入口。"""
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]) == {
        f"src/{name}" for name in PACKAGES
    }
    assert {path.name for path in (PROJECT_ROOT / "src").iterdir() if path.is_dir()} == PACKAGES
    root_names = {path.name for path in PROJECT_ROOT.iterdir()}
    assert {"src", "docs", "tests", "runs", "README.MD", ".env.example"} <= root_names
    assert config["project"]["readme"] == "README.MD"


def test_python_packages_resolve_from_source_root() -> None:
    """uv 开发安装的导入路径与磁盘上的 src/<模块> 一致。"""
    for name in PACKAGES:
        module = import_module(name)
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == PROJECT_ROOT / "src" / name / "__init__.py"


def test_console_entry_point_targets_interfaces() -> None:
    """已安装的入口元数据必须指向新的 interfaces 模块。"""
    entry_points = metadata.distribution("pi-claw").entry_points
    entry = next(item for item in entry_points if item.group == "console_scripts")
    assert entry.name == "pi-claw"
    assert entry.value == "interfaces.cli:main"
    assert entry.load() is import_module("interfaces.cli").main


def test_module_and_console_help_outside_workspace(tmp_path: Path) -> None:
    """从无源码的目录启动两种入口，避免仅在项目当前目录可用。"""
    console = Path(sysconfig.get_path("scripts")) / "pi-claw"
    commands = ([str(console)], [sys.executable, "-m", "interfaces"])
    for command in commands:
        completed = subprocess.run(
            [*command, "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        assert completed.returncode == 0, completed.stderr
        assert "eval-routing" in completed.stdout
        assert "chat" in completed.stdout


def test_repository_declares_mit_license() -> None:
    """公开仓库必须有 LICENSE 文件，并在包元数据里声明同样的许可证。"""
    license_file = PROJECT_ROOT / "LICENSE"
    assert license_file.is_file()
    text = license_file.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Permission is hereby granted" in text
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["license"] == "MIT"
    assert "LICENSE" in config["project"]["license-files"]
    assert "## 许可证" in (PROJECT_ROOT / "README.MD").read_text(encoding="utf-8")


def test_general_ci_workflow_covers_required_gates() -> None:
    """通用 CI 必须在推送/PR 上运行 Lint、格式、编译、测试与构建，且权限只读。"""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for fragment in (
        "push:",
        "pull_request:",
        "main",
        "optimize",
        "uv sync --frozen --all-groups",
        "ruff check src tests",
        "ruff format --check src tests",
        "compileall",
        "pytest",
        "uv build --no-config",
        "permissions:",
        "contents: read",
    ):
        assert fragment in workflow, fragment


def test_sandbox_workflow_is_separate_from_general_ci() -> None:
    """沙箱安全门与通用 CI 分工不同，不能互相吞并导致覆盖缺失。"""
    workflows = {
        path.name
        for path in (PROJECT_ROOT / ".github" / "workflows").iterdir()
        if path.suffix in {".yml", ".yaml"}
    }
    assert {"ci.yml", "sandbox.yml"} <= workflows
