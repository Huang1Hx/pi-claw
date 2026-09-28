"""路由评测自身的宿主验收与分类/执行调用分布，不用虚构价格得出节省率。"""

from evaluation.routing import evaluate_routing


def test_offline_complexity_routing_comparison(tmp_path):
    report = evaluate_routing(tmp_path / "report.json")
    assert report["passed_cases"] == report["total_cases"] == 6
    assert report["real_model"] is False
    assert report["suite"] == "routing-contract-v2"
    assert report["totals"]["auto"]["requests"] == 20
    assert report["totals"]["primary"]["requests"] == 12
    assert report["totals"]["auto"]["classification_requests"] == 6
    assert report["totals"]["primary"]["classification_requests"] == 0
    assert report["totals"]["auto"]["strong_execution_requests"] == 8
    assert report["totals"]["primary"]["strong_execution_requests"] == 12
    assert report["totals"]["auto"]["total_tokens"] == 2010
    assert report["totals"]["primary"]["total_tokens"] == 1440
    assert "cost_reduction_ratio" not in report
    assert "known_cost" not in report["totals"]["auto"]
    assert report["totals"]["auto"]["escalations"] == 2
