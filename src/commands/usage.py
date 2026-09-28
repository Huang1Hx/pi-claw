"""只读取本地持久账本，按当前会话和项目工作区展示已知用量，不查询供应商余额。"""


def _print_totals(repl, label: str, totals: dict[str, int]) -> None:
    """分开列出输入/输出/总量和统计缺口，已记录零值不等于供应商实际消耗为零。"""
    print(
        f"{label}｜已记录 Token：{totals.get('total_tokens', 0):,} "
        f"（输入 {totals.get('prompt_tokens', 0):,} / "
        f"输出 {totals.get('completion_tokens', 0):,}）\n"
        f"  有 usage 的响应：{totals.get('requests_with_usage', 0)}；"
        f"usage 未知的响应：{totals.get('requests_without_usage', 0)}；"
        f"请求重试：{totals.get('retries', 0)}",
        file=repl.output,
    )


def usage(repl, argument: str) -> bool:
    """项目按工作区真实路径划分；当前会话已包含在项目累计中，不能再次相加。"""
    if argument:
        raise ValueError("用法：/usage")
    report = repl.store.workspace_usage(repl.workspace)
    try:
        current = repl.store.total_usage(repl.session.session_id, repl.workspace)
    except (OSError, ValueError, TypeError, KeyError):
        print("当前会话｜账本不可读，用量未知（未按零计算）。", file=repl.output)
    else:
        _print_totals(repl, "当前会话", current)
        if not current:
            print("  尚无已记录回合；可能尚未调用模型或历史未记录。", file=repl.output)
    _print_totals(repl, "当前项目累计", report["totals"])
    print(
        f"  工作区：{repl.workspace}\n"
        f"  会话：{report['session_count']}；有记录 {report['recorded_sessions']}；"
        f"无记录 {report['unrecorded_sessions']}；不可读 {report['unreadable_sessions']}\n"
        "口径：同一工作区、同一会话存储中的历史累计，已包含当前会话；不包含其他工作区。\n"
        "仅统计已保存的服务端 usage；缺失统计、失败请求及未保存回合可能另有用量，不是账单或余额。",
        file=repl.output,
    )
    if report["unreadable_sessions"] or report["unrecorded_sessions"]:
        print("提示：无记录或不可读会话未纳入 Token 合计，累计值可能不完整。", file=repl.output)
    return True
