"""容器环境的离线单元测试：镜像映射、资源上限与 network none（不真的起容器）。"""

from __future__ import annotations

from types import SimpleNamespace

from external_eval.docker_env import LIMITS, docker_prepare, docker_tester, image_for


class _Recorder:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, cwd, stdin_text=None):
        self.calls.append(argv)
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def _instance(repo="psf/requests"):
    return {"instance_id": "psf__requests-5414", "repo": repo}


def test_image_for_uses_pinned_python_per_repo():
    assert image_for(_instance("psf/requests")) == "python:3.9-slim"
    assert image_for(_instance("pallets/flask")) == "python:3.8-slim"
    assert image_for(_instance("unknown/repo")) == "python:3.9-slim"


def test_docker_tester_pins_network_none_and_limits(tmp_path):
    recorder = _Recorder(returncode=0)
    tester = docker_tester(_instance(), tmp_path, runner=recorder)
    assert tester("tests/test_requests.py::TestX::test_y") is True
    argv = recorder.calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    for flag in LIMITS:
        assert flag in argv
    assert "-v" in argv and argv[-1].endswith("TestX::test_y")


def test_docker_tester_reports_failure_on_nonzero(tmp_path):
    tester = docker_tester(_instance(), tmp_path, runner=_Recorder(returncode=1))
    assert tester("tests/test_requests.py::TestX::test_y") is False


def test_docker_prepare_runs_install_in_one_container(tmp_path):
    recorder = _Recorder(returncode=0)
    interpreter = docker_prepare(_instance(), tmp_path, runner=recorder)
    assert interpreter == "/work/venv/bin/python"
    argv = recorder.calls[0]
    assert "pip install -q -e ." in argv[-1]
    assert "--network" not in argv
