"""终端会话控制命令；不进入 LLM 消息，也不执行 shell。"""


def compact(repl, argument: str) -> bool:
    """压缩并保存活动上下文。"""
    before = len(repl.messages)
    repl.messages[:] = repl.context.compact(repl.messages, argument or None)
    repl.store.save_active_messages(
        repl.session.session_id,
        repl.workspace,
        repl.messages,
        expected_seq=repl._session_seq,
    )
    print(f"上下文已压缩：{before} -> {len(repl.messages)} 条活动消息", file=repl.output)
    return True


def context(repl, argument: str) -> bool:
    """展示活动上下文大小。"""
    print(
        f"session={repl.session.session_id} messages={len(repl.messages)} "
        f"chars={repl.context.estimate_chars(repl.messages)} "
        f"tokens≈{repl.context.estimate_tokens(repl.messages)}",
        file=repl.output,
    )
    return True


def history(repl, argument: str) -> bool:
    """展示活动消息预览。"""
    repl._print_history()
    return True
