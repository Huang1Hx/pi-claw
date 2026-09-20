"""从命令注册元数据生成帮助，避免实现与提示不同步。"""


def help_command(repl, argument: str) -> bool:
    """显示可用命令及参数用法。"""
    for command in repl.commands.commands:
        print(f"{command.usage:<24} {command.description}", file=repl.output)
    return True
