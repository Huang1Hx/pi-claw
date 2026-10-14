from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.active_context import (
    ANCHOR_MAX_CHARS,
    MAX_PINS,
    SNAPSHOT_VERSION,
    ActiveContext,
    ActiveContextError,
    Pin,
    TaskAnchor,
)
from persistence.session_store import SessionStore


def _pin(pin_id: str, text: str) -> Pin:
    return Pin(pin_id=pin_id, text=text, seq=1, created_at="2026-10-14T00:00:00Z")


def test_empty_context_defaults_to_current_version() -> None:
    context = ActiveContext.empty()
    assert context.version == SNAPSHOT_VERSION == 3
    assert context.messages == []
    assert context.through_seq == 0
    assert context.snapshot_revision == 0
    assert context.pins == ()
    assert context.anchor is None


def test_payload_round_trips_pins_and_anchor() -> None:
    context = ActiveContext(
        messages=[{"role": "system", "content": "rules"}],
        through_seq=7,
        snapshot_revision=3,
        pins=(_pin("p1", "始终运行 pytest"), _pin("p2", "不要改动评分器")),
        anchor=TaskAnchor.build(
            text="目标：修复计费回归",
            covered_through_seq=7,
            sources=("task", "write-journal"),
            updated_at="2026-10-14T00:00:00Z",
        ),
    )
    payload = context.to_payload()
    assert payload["version"] == 3
    assert payload["pins"][0]["pin_id"] == "p1"
    assert payload["anchor"]["text"] == "目标：修复计费回归"
    assert ActiveContext.from_payload(payload) == context


def test_default_payload_omits_optional_fields() -> None:
    """无 pins/anchor 时快照只含基础字段，体积与 v2 保持等价。"""
    assert ActiveContext.empty().to_payload() == {
        "version": 3,
        "through_seq": 0,
        "messages": [],
    }


def test_v2_payload_upgrades_with_empty_pins_and_anchor() -> None:
    context = ActiveContext.from_payload(
        {"version": 2, "through_seq": 5, "messages": [{"role": "system", "content": "s"}]}
    )
    assert context.version == SNAPSHOT_VERSION
    assert context.through_seq == 5
    assert context.snapshot_revision == 0
    assert context.pins == ()
    assert context.anchor is None


@pytest.mark.parametrize("payload", [{"version": 4}, {"version": "3"}, [], None, 3])
def test_unsupported_payload_versions_are_rejected(payload: object) -> None:
    with pytest.raises(ActiveContextError):
        ActiveContext.from_payload(payload)


def test_pins_validation_rejects_duplicates_too_many_and_oversized() -> None:
    with pytest.raises(ActiveContextError, match="重复"):
        ActiveContext(messages=[], pins=(_pin("dup", "a"), _pin("dup", "b")))
    many = tuple(_pin(f"p{i}", "x") for i in range(MAX_PINS + 1))
    with pytest.raises(ActiveContextError, match="数量"):
        ActiveContext(messages=[], pins=many)
    with pytest.raises(ActiveContextError, match="长度"):
        ActiveContext(messages=[], pins=(_pin("long", "y" * 10_000),))


def test_anchor_length_limit_is_enforced() -> None:
    with pytest.raises(ActiveContextError, match="anchor"):
        TaskAnchor.build(text="z" * (ANCHOR_MAX_CHARS + 1), covered_through_seq=1)


def test_anchor_content_hash_is_deterministic_and_input_sensitive() -> None:
    first = TaskAnchor.build(text="goal", covered_through_seq=3, sources=("task", "verifier"))
    second = TaskAnchor.build(text="goal", covered_through_seq=3, sources=("verifier", "task"))
    changed = TaskAnchor.build(text="goal v2", covered_through_seq=3, sources=("task", "verifier"))
    assert first.content_hash == second.content_hash
    assert first.content_hash != changed.content_hash


def test_with_messages_preserves_pins_and_anchor_and_versions_in_place_edits() -> None:
    context = ActiveContext(
        messages=[{"role": "system", "content": "rules"}],
        through_seq=1,
        snapshot_revision=4,
        pins=(_pin("p1", "keep"),),
        anchor=TaskAnchor.build(text="goal", covered_through_seq=1, sources=("task",)),
    )
    compacted = context.with_messages([{"role": "system", "content": "rules v2"}], through_seq=1)
    assert compacted.snapshot_revision == 5  # 水位线不变、内容变化 -> 原地编辑
    assert compacted.pins == context.pins
    assert compacted.anchor == context.anchor

    appended = context.with_messages(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "next"}],
        through_seq=9,
    )
    assert appended.snapshot_revision == 4  # 水位线推进 -> 由 seq 保护，不重复计数
    assert appended.through_seq == 9


def test_store_message_save_does_not_erase_pins_or_anchor(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    messages, seq = store.load_active_snapshot(session.session_id, workspace)
    context = ActiveContext(
        messages=messages,
        through_seq=seq,
        snapshot_revision=1,
        pins=(_pin("p1", "始终运行 pytest"),),
        anchor=TaskAnchor.build(text="目标：修复回归", covered_through_seq=seq, sources=("task",)),
    )
    store.save_active_context(session.session_id, workspace, context)

    compacted = messages + [{"role": "assistant", "content": "压缩摘要"}]
    store.save_active_messages(session.session_id, workspace, compacted)

    reloaded = store.load_active_context(session.session_id, workspace)
    assert reloaded.messages == compacted
    assert [pin.text for pin in reloaded.pins] == ["始终运行 pytest"]
    assert reloaded.anchor is not None and reloaded.anchor.text == "目标：修复回归"
    assert reloaded.snapshot_revision == 2  # 原地压缩推进修订号


def test_store_active_context_round_trip_and_seq_conflict(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    messages, seq = store.load_active_snapshot(session.session_id, workspace)
    context = ActiveContext(messages=messages, through_seq=seq, snapshot_revision=1)
    store.save_active_context(session.session_id, workspace, context)

    loaded = store.load_active_context(session.session_id, workspace)
    assert loaded.through_seq == seq
    assert loaded.messages == messages

    store.append_message(session.session_id, workspace, {"role": "user", "content": "next"})
    with pytest.raises(RuntimeError, match="另一个进程"):
        store.save_active_context(
            session.session_id, workspace, context.with_pins(()), expected_seq=seq
        )


def test_v2_snapshot_loads_through_active_context(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    (session_dir / "active_context.json").write_text(
        json.dumps({"version": 2, "through_seq": 0, "messages": []}),
        encoding="utf-8",
    )
    context = store.load_active_context(session.session_id, workspace)
    assert context.version == SNAPSHOT_VERSION
    assert context.messages == [{"role": "system", "content": "system"}]
    assert context.pins == ()


def _legacy_v2_only_reader(value: object) -> object:
    """模拟只认识 v2 的旧 checkpoint 读取分支，用于验证降级为显式失败。"""

    if not isinstance(value, dict) or value.get("version") != 2:
        raise ValueError("会话活动上下文版本不受支持")
    return value


def test_v3_snapshot_is_rejected_by_v2_only_reader(workspace: Path, tmp_path: Path) -> None:
    """旧程序读到 v3 必须显式报不支持，不能静默当作 v2 忽略 pins/anchor。"""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    payload = json.loads((session_dir / "active_context.json").read_text(encoding="utf-8"))
    assert payload["version"] == 3
    with pytest.raises(ValueError, match="版本"):
        _legacy_v2_only_reader(payload)


def test_unknown_snapshot_version_fails_before_any_write(workspace: Path, tmp_path: Path) -> None:
    """未知版本在写入前预检失败：快照不被改写，journal 也不增长。"""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    active = session_dir / "active_context.json"
    active.write_text(
        json.dumps({"version": 4, "through_seq": 1, "messages": []}),
        encoding="utf-8",
    )
    corrupted = active.read_bytes()
    before_journal = store.load_messages(session.session_id, workspace)

    with pytest.raises(ValueError, match="版本"):
        store.append_message(session.session_id, workspace, {"role": "user", "content": "x"})
    with pytest.raises(ValueError, match="版本"):
        store.save_active_messages(session.session_id, workspace, [])

    assert active.read_bytes() == corrupted
    assert store.load_messages(session.session_id, workspace) == before_journal


def test_interrupted_recovery_then_save_does_not_duplicate_messages(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中断→恢复→保存→再恢复后消息不重复，水位线同步到实际吸收位置。"""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    original = store._save_active_messages_unlocked

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash")

    monkeypatch.setattr(store, "_save_active_messages_unlocked", crash)
    with pytest.raises(OSError):
        store.append_message(session.session_id, workspace, {"role": "user", "content": "tail"})
    monkeypatch.setattr(store, "_save_active_messages_unlocked", original)

    recovered = store.load_active_context(session.session_id, workspace)
    assert len(recovered.messages) == 2
    assert recovered.through_seq == 2  # 已吸收 journal 尾部，水位线必须同步

    store.save_active_context(session.session_id, workspace, recovered)
    again = store.load_active_context(session.session_id, workspace)
    assert [m.get("content") for m in again.messages] == ["system", "tail"]


def test_save_active_context_prechecks_existing_version(workspace: Path, tmp_path: Path) -> None:
    """新保存接口在写入前预检既有快照版本，未知版本 fail closed 且不改写文件。"""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    session_dir = next(store.data_dir.glob(f"*/{session.session_id}"))
    active = session_dir / "active_context.json"
    active.write_text(
        json.dumps({"version": 4, "through_seq": 1, "messages": []}), encoding="utf-8"
    )
    corrupted = active.read_bytes()

    with pytest.raises(ValueError, match="版本"):
        store.save_active_context(session.session_id, workspace, ActiveContext.empty())

    assert active.read_bytes() == corrupted


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 3, "through_seq": 1},
        {"version": 3, "messages": []},
        {"version": 2},
    ],
)
def test_missing_required_fields_are_rejected(payload: dict[str, object]) -> None:
    """messages/through_seq 是必填字段，缺失时不能默认成空数据。"""
    with pytest.raises(ActiveContextError, match="messages|through_seq"):
        ActiveContext.from_payload(payload)


def test_save_active_context_rejects_oversized_and_ahead_of_journal(
    workspace: Path, tmp_path: Path
) -> None:
    """保存完整上下文前拒绝超预算和超前水位线，不能写坏有效快照。"""
    store = SessionStore(tmp_path / "sessions", max_events=1)
    session = store.create(workspace, "system")
    messages, seq = store.load_active_snapshot(session.session_id, workspace)

    with pytest.raises(ValueError, match="消息上限"):
        store.save_active_context(
            session.session_id,
            workspace,
            ActiveContext(messages=messages + [{"role": "user", "content": "x"}], through_seq=seq),
        )
    with pytest.raises(ValueError, match="领先"):
        store.save_active_context(
            session.session_id,
            workspace,
            ActiveContext(messages=messages, through_seq=seq + 5),
        )

    assert store.load_active_context(session.session_id, workspace).through_seq == seq
