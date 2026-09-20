"""终端会话控制命令；不进入 LLM 消息，也不执行 shell。"""


def exit_command(repl, argument: str) -> bool:
    """退出会话。"""
    return False
