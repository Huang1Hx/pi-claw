"""离线路由校准的加载、fail-closed 与复杂任务错误降级回归门。"""

import json

import pytest

from evaluation.routing_calibration import (
    evaluate_routing_calibration,
    load_calibration_suite,
)
from interfaces.cli import main


def _suite(tmp_path, cases, **overrides):
    payload = {"schema_version": 1, "name": "校准", "version": 1, "cases": cases}
    payload.update(overrides)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_default_dataset_reports_accuracy_and_downgrades(tmp_path):
    """标明离线与固定规则，并给出准确率、错误降级与保守升级的真实计数。"""
    report = evaluate_routing_calibration(tmp_path / "report.json")
    assert report["suite"] == "routing-calibration-v1"
    assert report["real_model"] is False
    assert report["total_cases"] == 28
    assert report["simple_cases"] == 9
    assert report["complex_cases"] == 19
    assert report["rules_accuracy"] == round(26 / 28, 6)
    assert report["rules_false_downgrades"] == 0
    assert report["rules_known_downgrades"] == 0
    assert report["rules_unexpected_downgrades"] == 0
    assert report["rules_false_escalations"] == 2
    assert report["guarded_false_downgrades"] == 0
    assert "cost_reduction_ratio" not in report
    assert (tmp_path / "report.json").exists()


def test_report_rows_expose_reason_and_collateral_cost(tmp_path):
    """逐条保留可审计原因；写入副作用已修复，只读误伤作为保守升级成本可见。"""
    report = evaluate_routing_calibration(tmp_path / "report.json")
    by_id = {row["id"]: row for row in report["cases"]}
    fixed = by_id["read_data_write_output"]
    assert fixed["label"] == "complex"
    assert fixed["rules_decision"] == "complex"
    assert fixed["false_downgrade"] is False
    assert fixed["reason"]
    assert by_id["view_write_log"]["false_escalation"] is True
    assert by_id["read_readme_with_space"]["false_escalation"] is True
    assert by_id["fix_login"]["guarded_decision"] == "complex"
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == report


def test_known_downgrade_is_counted_separately_from_unexpected(tmp_path):
    """带 known_downgrade 标记的规则盲点计入 known，未标记的计入必须为 0 的回归门。"""
    path = _suite(
        tmp_path,
        [
            {"id": "accepted", "task": "读取文件", "label": "complex", "known_downgrade": True},
            {"id": "regression", "task": "读取数据", "label": "complex"},
        ],
    )
    report = evaluate_routing_calibration(tmp_path / "custom.json", suite=path)
    assert report["rules_false_downgrades"] == 2
    assert report["rules_known_downgrades"] == 1
    assert report["rules_unexpected_downgrades"] == 1


def test_dataset_is_fail_closed_on_duplicate_id(tmp_path):
    """重复 id 必须拒绝，避免同一任务被重复计数扭曲准确率。"""
    path = _suite(
        tmp_path,
        [
            {"id": "a", "task": "列出项目文件", "label": "simple"},
            {"id": "a", "task": "读取alpha.txt", "label": "simple"},
        ],
    )
    with pytest.raises(ValueError, match="重复"):
        load_calibration_suite(path)


def test_dataset_is_fail_closed_on_bad_label(tmp_path):
    """非法标签必须拒绝，避免把无法判定对错的样本混入度量。"""
    path = _suite(tmp_path, [{"id": "a", "task": "列出项目文件", "label": "maybe"}])
    with pytest.raises(ValueError, match="label"):
        load_calibration_suite(path)


def test_known_downgrade_requires_complex_label(tmp_path):
    """known_downgrade 只对复杂任务有意义，标在简单任务上是数据错误。"""
    path = _suite(
        tmp_path,
        [{"id": "a", "task": "列出项目文件", "label": "simple", "known_downgrade": True}],
    )
    with pytest.raises(ValueError, match="known_downgrade"):
        load_calibration_suite(path)


def test_dataset_is_fail_closed_on_unknown_schema(tmp_path):
    """未知 schema 版本必须拒绝，防止静默按错误格式解读数据集。"""
    path = _suite(tmp_path, [{"id": "a", "task": "列出项目文件", "label": "simple"}])
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"schema_version": 1', '"schema_version": 2'), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_calibration_suite(path)


def test_cli_calibration_command_is_offline_and_passes(tmp_path):
    """命令行入口离线运行，安全回归门通过时返回 0。"""
    output = tmp_path / "cli.json"
    assert main(["eval-routing-calibration", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["rules_unexpected_downgrades"] == 0
