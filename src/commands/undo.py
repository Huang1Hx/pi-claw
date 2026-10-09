"""显式撤销当前会话最近一次已提交的宿主写入，不静默回滚。"""


def undo(repl, argument: str) -> bool:
    """只撤销最近一条写入记录；缺失、外部改动与失败都给出明确说明，绝不覆盖。"""
    if argument:
        raise ValueError("用法：/undo")
    agent = getattr(repl, "agent", None)
    journal = getattr(getattr(agent, "tool_executor", None), "journal", None)
    if agent is None or journal is None:
        print("当前会话没有可撤销的宿主写入记录。", file=repl.output)
        return True
    summary = agent.undo_last()
    if summary["restored"] == 0 and summary["deleted"] == 0 and summary["skipped"] == 0:
        print("没有可撤销的宿主写入记录。", file=repl.output)
        return True
    print(
        f"撤销：恢复 {summary['restored']}，删除 {summary['deleted']}，"
        f"跳过 {summary['skipped']}，失败 {summary['failed']}",
        file=repl.output,
    )
    if summary["skipped"]:
        print(
            "部分改动因缺失或已被外部改动而跳过，未覆盖外部状态；详见回滚 Trace。",
            file=repl.output,
        )
    if summary["failed"]:
        print("存在回滚失败项，已如实记录，未伪装成功。", file=repl.output)
    return True
