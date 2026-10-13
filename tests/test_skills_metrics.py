"""Skill Trace 指标聚合。"""

from skills.metrics import count_events, lifecycle_completeness


def ev(name, **payload):
    return {"event": name, "payload": payload}


def test_count_events():
    events = [
        ev("skill_selected"),
        ev("skill_started"),
        ev("skill_completed"),
        ev("other"),
    ]
    counts = count_events(events)
    assert counts["skill_selected"] == 1 and counts["skill_completed"] == 1
    assert counts["skill_failed"] == 0


def test_lifecycle_completeness_full_and_partial():
    assert lifecycle_completeness([ev("skill_started"), ev("skill_completed")]) == 1.0
    assert lifecycle_completeness([ev("skill_started"), ev("skill_failed")]) == 1.0
    assert (
        lifecycle_completeness([ev("skill_started"), ev("skill_started"), ev("skill_completed")])
        == 0.5
    )
    # 没有激活过任何 Skill 时视为完整
    assert lifecycle_completeness([ev("skill_rejected")]) == 1.0
