"""交互命令注册、别名和派发；help 由同一元数据生成。"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    """命令名、说明与处理函数。

    handler(repl, argument) 接收当前交互对象和命令后的原始文本；True 继续，False 退出。
    命令属于宿主交互层，不是 LLM 工具，也不是拼接后交给 shell 的命令行。
    """

    name: str
    usage: str
    description: str
    handler: Callable
    aliases: tuple[str, ...] = ()


class CommandRegistry:
    """启动时显式注册内置命令，拒绝重名和别名冲突。"""

    def __init__(self) -> None:
        """创建命令列表与名称索引；别名和主命令共用同一个处理对象。"""
        self.commands: list[Command] = []
        self.lookup: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        """先检查所有名称，再一起更新列表和索引；本注册表不提供并发写锁。"""
        names = (command.name, *command.aliases)
        if len(set(names)) != len(names) or any(n in self.lookup for n in names):
            raise ValueError("命令名或别名冲突")
        self.commands.append(command)
        self.lookup.update({name: command for name in names})

    def dispatch(self, raw: str, repl) -> bool:
        """命令错误显示后继续交互，不落入 Agent 当作任务。"""
        parts = raw.strip().split(maxsplit=1)
        if not parts:
            return True
        if parts[0] == "/":
            # 裸斜杠是本地帮助入口，非 TTY 没有弹出菜单时同样可用。
            return self.lookup["/help"].handler(repl, "") if "/help" in self.lookup else True
        command = self.lookup.get(parts[0])
        if command is None:
            print(f"未知命令：{parts[0]}。输入 /help 查看命令。", file=repl.output)
            matches = [name for name in self.lookup if name.startswith(parts[0])]
            if matches:
                print("可补全：" + "、".join(matches), file=repl.output)
            return True
        try:
            return command.handler(repl, parts[1].strip() if len(parts) == 2 else "")
        except (ValueError, OSError, TypeError) as exc:
            print(f"命令错误：{exc}", file=repl.output)
            return True
