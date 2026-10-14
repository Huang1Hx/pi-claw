"""终端会话控制命令；不进入 LLM 消息，也不执行 shell。"""

from __future__ import annotations

from core.active_context import MAX_PINS
from core.task_anchor import ANCHOR_PINS_BUDGET
from persistence.factory import store_backend


def compact(repl, argument: str) -> bool:
    """压缩并保存活动上下文；保留 pins/goal/锚，并记录结构化事件。"""
    before = len(repl.messages)
    compacted = repl.context.compact(repl.messages, argument or None)
    if compacted == repl.messages:
        print(f"上下文无需压缩（{before} 条活动消息）。", file=repl.output)
        return True
    repl.messages[:] = compacted
    try:
        repl.store.save_active_messages(
            repl.session.session_id,
            repl.workspace,
            repl.messages,
            expected_seq=repl._session_seq,
        )
    except (OSError, ValueError):
        repl.trace.record("session_storage_failed", {"session_id": repl.session.session_id})
        raise
    repl.trace.record(
        "context_compacted",
        {
            "session_id": repl.session.session_id,
            "before_messages": before,
            "after_messages": len(repl.messages),
        },
    )
    print(f"上下文已压缩：{before} -> {len(repl.messages)} 条活动消息", file=repl.output)
    return True


def context(repl, argument: str) -> bool:
    """展示活动上下文大小、水位线、修订号与锚/约束构成。"""
    if argument.strip():
        raise ValueError("用法：/context")
    active = repl.load_context()
    budget = repl.context
    print(
        f"session={repl.session.session_id} 活动消息={len(repl.messages)} "
        f"字符={repl.context.estimate_chars(repl.messages)} "
        f"tokens≈{repl.context.estimate_tokens(repl.messages)}",
        file=repl.output,
    )
    print(f"会话后端：{store_backend(repl.store)}", file=repl.output)
    print(
        f"水位线：through_seq={active.through_seq} "
        f"修订号：snapshot_revision={active.snapshot_revision}",
        file=repl.output,
    )
    print(
        f"预算：messages≤{budget.max_messages} chars≤{budget.max_chars} "
        f"pins≤{MAX_PINS} 条/总长≤{ANCHOR_PINS_BUDGET} 字符",
        file=repl.output,
    )
    if active.goal:
        print(f"目标：{active.goal}", file=repl.output)
    if active.pins:
        print(
            "约束：" + "；".join(f"{pin.pin_id}={pin.text}" for pin in active.pins),
            file=repl.output,
        )
    else:
        print("约束：无（/pin <内容> 添加）", file=repl.output)
    if active.anchor is not None:
        print("任务锚：\n" + active.anchor.text, file=repl.output)
    else:
        print("任务锚：无", file=repl.output)
    return True


def history(repl, argument: str) -> bool:
    """展示活动消息预览。"""
    repl._print_history()
    return True
