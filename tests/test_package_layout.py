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
