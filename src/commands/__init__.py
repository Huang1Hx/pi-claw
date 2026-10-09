"""终端命令目录；与 tools/shell.py 的受控子进程工具分开。"""

from commands.context import compact, context, history
from commands.exit import exit_command
from commands.help import help_command
from commands.model import model
from commands.registry import Command, CommandRegistry
from commands.sessions import new_session, resume, sessions, title
from commands.status import status
from commands.undo import undo
from commands.usage import usage


def create_commands() -> CommandRegistry:
    """显式注册内置命令，不执行目录中的第三方文件。"""
    registry = CommandRegistry()
    for spec in [
        Command("/help", "/help", "显示命令帮助", help_command),
        Command("/status", "/status [--verbose]", "简明状态；可查看调试详情", status),
        Command("/model", "/model [模型ID|auto|别名]", "查看具体模型；切换时新建会话", model),
        Command("/exit", "/exit", "退出交互", exit_command),
        Command("/sessions", "/sessions", "列出工作区会话", sessions),
        Command("/resume", "/resume <id>", "恢复会话", resume),
        Command("/new", "/new", "新建会话", new_session),
        Command("/title", "/title <标题>", "修改标题", title),
        Command("/compact", "/compact [关注点]", "压缩上下文", compact),
        Command("/context", "/context", "上下文大小", context),
        Command("/usage", "/usage", "当前会话和项目累计 Token", usage),
        Command("/undo", "/undo", "撤销最近一次宿主写入", undo),
        Command("/history", "/history", "查看消息预览", history),
    ]:
        registry.register(spec)
    return registry
