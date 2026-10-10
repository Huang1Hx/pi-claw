"""外部评测的离线单元测试：筛选、隔离与判定逻辑（不联网、不碰冻结评测）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from external_eval.harness import create_venv, evaluate_patch, parse_junit
from external_eval.swebench_verified import (
    assert_isolated_output,
    frozen_task_ids,
    instance_tests,
    load_selection,
    select_instances,
)


def _meta(instance_id, repo="psf/requests", ftp=("tests/test_a.py::test_x",), ptp=()):
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": "deadbeef",
        "patch": "diff --git a/x b/x\n",
        "test_patch": "diff --git a/t b/t\n",
        "FAIL_TO_PASS": json.dumps(list(ftp)),
        "PASS_TO_PASS": json.dumps(list(ptp)),
    }


def test_selection_is_reproducible_and_within_allowed_repos():
    selection = load_selection()
    assert len(selection["instances"]) >= 10
    rows = select_instances(
        [_meta(i, repo="psf/requests") for i in selection["instances"]], selection
    )
    assert [r["instance_id"] for r in rows] == selection["instances"]


def test_selection_rejects_missing_and_off_repo_instances():
    selection = {"instances": ["psf__requests-1"], "repos": ["psf/requests"]}
    with pytest.raises(ValueError):
        select_instances([_meta("psf__requests-2")], selection)
    with pytest.raises(ValueError):
        select_instances([_meta("psf__requests-1", repo="django/django")], selection)


def test_instance_tests_accepts_json_strings_and_lists():
    parsed = instance_tests(_meta("x", ftp=("a::t",), ptp=("b::t", "c::t")))
    assert parsed == {"FAIL_TO_PASS": ["a::t"], "PASS_TO_PASS": ["b::t", "c::t"]}


def test_isolated_output_blocks_frozen_batch_prefixes(tmp_path):
    ok = assert_isolated_output(tmp_path / "swebench-verified" / "work")
    assert ok.name == "work"
    with pytest.raises(ValueError):
        assert_isolated_output(tmp_path / "stage3-regression")
    with pytest.raises(ValueError):
        assert_isolated_output(tmp_path / "other" / "plain")


def test_external_instances_never_overlap_frozen_task_ids():
    selection = load_selection()
    assert set(selection["instances"]).isdisjoint(frozen_task_ids())


class _FakeRunner:
    """对所有命令返回成功的假执行器（仅用于 git 重置/应用补丁）。"""

    def __call__(self, argv, cwd, stdin_text=None):
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _tester(fail_ids):
    """把"失败测试集合"包装成 test_fn(test_id) -> bool。"""
    return lambda test_ids: {t: t not in fail_ids for t in test_ids}


def test_parse_junit_maps_classname_to_nodeid(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("")
    xml = (
        '<testsuites><testsuite name="pytest" tests="3">'
        '<testcase classname="tests.test_mod" name="test_fn"/>'
        '<testcase classname="tests.test_mod.TestCls" name="test_m[1]">'
        '<failure message="boom"/></testcase>'
        '<testcase classname="tests.test_mod.TestCls" name="test_ok[2]"/>'
        "</testsuite></testsuites>"
    )
    parsed = parse_junit(xml, tmp_path)
    assert parsed == {
        "tests/test_mod.py::test_fn": True,
        "tests/test_mod.py::TestCls::test_m[1]": False,
        "tests/test_mod.py::TestCls::test_ok[2]": True,
    }


def _instance():
    return _meta("psf__requests-1", ftp=("t::a", "t::b"), ptp=("t::p",))


def test_evaluate_patch_resolves_only_when_all_tests_pass(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _FakeRunner()
    good = evaluate_patch(_instance(), repo, _tester(set()), "diff", runner=runner)
    assert good["resolved"] is True
    assert good["fail_to_pass"] == {"t::a": True, "t::b": True}

    broken_ftp = evaluate_patch(_instance(), repo, _tester({"t::b"}), "diff", runner=runner)
    assert broken_ftp["resolved"] is False

    broken_ptp = evaluate_patch(_instance(), repo, _tester({"t::p"}), "diff", runner=runner)
    assert broken_ptp["resolved"] is False


def test_create_venv_can_pin_python_version_with_uv(tmp_path):
    """指定版本时必须走 uv venv/pip，避免用宿主 3.12 跑旧仓库造成环境噪声。"""
    seen = []

    def runner(argv, cwd, stdin_text=None):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    repo = tmp_path / "repo"
    repo.mkdir()
    create_venv(repo, runner=runner, python_version="3.9")
    assert ["uv", "venv", "--python", "3.9", str(repo.parent / "venv")] in seen
    assert any(argv[:3] == ["uv", "pip", "install"] for argv in seen)
