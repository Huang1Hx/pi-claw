"""M2：确定性任务锚构建。"""

from __future__ import annotations

import socket
import subprocess

import pytest

from core.active_context import ANCHOR_MAX_CHARS
from core.task_anchor import AnchorInputs, build_anchor


def test_same_inputs_produce_identical_text_and_hash() -> None:
    inputs = AnchorInputs(
        task="修复计费回归",
        pins=("不要改动评分器", "始终运行 pytest"),
        changed_files=("src/billing.py",),
        verification_accepted=True,
        verification_reason="pytest 通过",
        workspace_fingerprint="abc",
        verified_workspace_fingerprint="abc",
        covered_through_seq=7,
    )
    first = build_anchor(inputs, updated_at="2026-10-14T00:00:00Z")
    second = build_anchor(inputs, updated_at="2026-10-15T09:09:09Z")

    assert first is not None and second is not None
    assert first.text == second.text
    assert first.content_hash == second.content_hash  # updated_at 不参与确定性比较
    assert first.covered_through_seq == 7


def test_anchor_is_deterministic_across_repeated_builds() -> None:
    inputs = AnchorInputs(task="任务", pins=("约束",))
    assert build_anchor(inputs).text == build_anchor(inputs).text


def test_task_switch_replaces_old_goal() -> None:
    old = build_anchor(AnchorInputs(task="旧目标：修 A"))
    new = build_anchor(AnchorInputs(task="新目标：修 B"))
    assert old is not None and new is not None
    assert "修 A" not in new.text
    assert "修 B" in new.text
    assert old.content_hash != new.content_hash


def test_revoked_pin_disappears_from_anchor() -> None:
    with_pin = build_anchor(AnchorInputs(task="任务", pins=("约束一", "约束二")))
    without = build_anchor(AnchorInputs(task="任务", pins=("约束一",)))
    assert with_pin is not None and without is not None
    assert "约束二" in with_pin.text
    assert "约束二" not in without.text
    assert with_pin.content_hash != without.content_hash


def test_incomplete_write_journal_is_marked_explicitly() -> None:
    anchor = build_anchor(
        AnchorInputs(
            task="任务",
            changed_files=("a.py", "b.py"),
            changed_files_incomplete=True,
            omitted_changed_files=3,
        )
    )
    assert anchor is not None
    assert "不完整" in anchor.text
    assert "3" in anchor.text  # 省略数量显式可见


def test_verification_is_invalidated_after_new_writes() -> None:
    fresh = build_anchor(
        AnchorInputs(
            task="任务",
            changed_files=("a.py",),
            verification_accepted=True,
            verification_reason="pytest 通过",
            workspace_fingerprint="v2",
            verified_workspace_fingerprint="v2",
        )
    )
    stale = build_anchor(
        AnchorInputs(
            task="任务",
            changed_files=("a.py",),
            verification_accepted=True,
            verification_reason="pytest 通过",
            workspace_fingerprint="v3",
            verified_workspace_fingerprint="v2",
        )
    )
    assert fresh is not None and stale is not None
    assert "失效" in stale.text and "失效" not in fresh.text
    assert fresh.content_hash != stale.content_hash


def test_empty_inputs_return_none() -> None:
    assert build_anchor(AnchorInputs()) is None
    assert build_anchor(AnchorInputs(task="   ")) is None


def test_anchor_is_bounded_and_never_silently_drops_constraints() -> None:
    anchor = build_anchor(AnchorInputs(task="目标 " * 2_000, pins=("关键约束",)))
    assert anchor is not None
    assert len(anchor.text) <= ANCHOR_MAX_CHARS
    assert "关键约束" in anchor.text  # 硬约束不被截断

    with pytest.raises(ValueError, match="约束"):
        build_anchor(AnchorInputs(task="任务", pins=("x" * (ANCHOR_MAX_CHARS + 1),)))


def test_build_anchor_is_pure_without_external_io(monkeypatch) -> None:
    """构建只依赖入参：禁止触达网络或子进程，且重复构建结果一致。"""

    def boom(*_args, **_kwargs):
        raise AssertionError("锚构建不应触达外部 IO")

    monkeypatch.setattr(socket, "socket", boom, raising=False)
    monkeypatch.setattr(socket, "create_connection", boom, raising=False)
    monkeypatch.setattr(subprocess, "Popen", boom, raising=False)
    monkeypatch.setattr(subprocess, "run", boom, raising=False)

    first = build_anchor(AnchorInputs(task="t", pins=("p",)))
    second = build_anchor(AnchorInputs(task="t", pins=("p",)))
    assert first is not None and second is not None
    assert first.content_hash == second.content_hash


# ---- M2-B：文件后端修订号 CAS ----

from core.active_context import Pin  # noqa: E402
from persistence.session_store import SessionStore  # noqa: E402


def _seed(workspace, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(workspace, "system")
    return store, session


def test_revision_cas_rejects_concurrent_pin_update(workspace, tmp_path) -> None:
    """两个实例基于同一修订号各自加 pin，只能有一个提交。"""
    store, session = _seed(workspace, tmp_path)
    _messages, seq = store.load_active_snapshot(session.session_id, workspace)
    first = store.load_active_context(session.session_id, workspace)
    second = store.load_active_context(session.session_id, workspace)

    store.save_active_context(
        session.session_id,
        workspace,
        first.with_pins((Pin(pin_id="p1", text="one", seq=seq),)),
        expected_seq=seq,
        expected_revision=first.snapshot_revision,
    )
    with pytest.raises(RuntimeError, match="另一个进程"):
        store.save_active_context(
            session.session_id,
            workspace,
            second.with_pins((Pin(pin_id="p2", text="two", seq=seq),)),
            expected_seq=seq,
            expected_revision=second.snapshot_revision,
        )

    loaded = store.load_active_context(session.session_id, workspace)
    assert [pin.pin_id for pin in loaded.pins] == ["p1"]


def test_revision_cas_rejects_concurrent_compaction(workspace, tmp_path) -> None:
    """相同 seq 的两次本地压缩只能有一个提交。"""
    store, session = _seed(workspace, tmp_path)
    messages, seq = store.load_active_snapshot(session.session_id, workspace)
    revision = store.load_active_context(session.session_id, workspace).snapshot_revision

    store.save_active_messages(
        session.session_id,
        workspace,
        messages + [{"role": "assistant", "content": "A"}],
        expected_seq=seq,
        expected_revision=revision,
    )
    with pytest.raises(RuntimeError, match="另一个进程"):
        store.save_active_messages(
            session.session_id,
            workspace,
            messages + [{"role": "assistant", "content": "B"}],
            expected_seq=seq,
            expected_revision=revision,
        )


def test_expected_revision_is_scoped_to_in_place_updates(workspace, tmp_path) -> None:
    """CAS 只用于水位线不变的原地更新：追加后携带旧修订号保存必须显式失败。

    追加（水位线前进）由 expected_seq 保护；若允许其携带旧修订号通过，
    两个写者会在同一修订号上先后覆盖，CAS 形同虚设。
    """
    store, session = _seed(workspace, tmp_path)
    _messages, seq = store.load_active_snapshot(session.session_id, workspace)
    revision = store.load_active_context(session.session_id, workspace).snapshot_revision

    store.append_message(session.session_id, workspace, {"role": "user", "content": "tail"})
    messages, new_seq = store.load_active_snapshot(session.session_id, workspace)

    with pytest.raises(ValueError, match="snapshot_revision"):
        store.save_active_messages(
            session.session_id,
            workspace,
            messages,
            expected_seq=new_seq,
            expected_revision=revision,
        )
    # 追加路径只用 expected_seq 即可安全提交。
    store.save_active_messages(
        session.session_id,
        workspace,
        messages,
        expected_seq=new_seq,
    )


def test_anchor_survives_interrupted_append_then_recovery(workspace, tmp_path, monkeypatch) -> None:
    """快照已含锚；追加时中断后恢复，锚仍在且 journal 尾部不丢。"""
    from core.active_context import TaskAnchor

    store, session = _seed(workspace, tmp_path)
    messages, seq = store.load_active_snapshot(session.session_id, workspace)
    context = store.load_active_context(session.session_id, workspace).with_anchor(
        TaskAnchor.build(text="任务锚：目标=修复回归", covered_through_seq=seq, sources=("task",))
    )
    store.save_active_context(session.session_id, workspace, context)

    original = store._save_active_messages_unlocked

    def crash(*_args, **_kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(store, "_save_active_messages_unlocked", crash)
    with pytest.raises(OSError):
        store.append_message(session.session_id, workspace, {"role": "user", "content": "tail"})
    monkeypatch.setattr(store, "_save_active_messages_unlocked", original)

    recovered = store.load_active_context(session.session_id, workspace)
    assert recovered.anchor is not None and recovered.anchor.text == "任务锚：目标=修复回归"
    assert [m.get("content") for m in recovered.messages] == ["system", "tail"]


def test_legacy_message_api_preserves_anchor_and_bumps_revision(workspace, tmp_path) -> None:
    """兼容包装器 save_active_messages 不得擦除锚，且原地压缩推进修订号。"""
    from core.active_context import TaskAnchor

    store, session = _seed(workspace, tmp_path)
    messages, seq = store.load_active_snapshot(session.session_id, workspace)
    context = store.load_active_context(session.session_id, workspace).with_anchor(
        TaskAnchor.build(text="任务锚：目标=修复回归", covered_through_seq=seq, sources=("task",))
    )
    store.save_active_context(session.session_id, workspace, context)
    before = store.load_active_context(session.session_id, workspace).snapshot_revision

    store.save_active_messages(
        session.session_id,
        workspace,
        messages + [{"role": "assistant", "content": "压缩摘要"}],
    )

    after = store.load_active_context(session.session_id, workspace)
    assert after.anchor is not None and after.anchor.text == "任务锚：目标=修复回归"
    assert after.snapshot_revision == before + 1
