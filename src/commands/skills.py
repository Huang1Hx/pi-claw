"""Skill 交互命令；/skill 只接受内置 ID，绝不按文件路径加载。"""

_PATH_CHARS = ("/", "\\", "..")


def skills(repl, argument: str) -> bool:
    """列出内置 Skill 的说明与权限属性。"""
    if argument.strip():
        raise ValueError("用法：/skills")
    entries = repl.skills.list()
    if not entries:
        print("当前没有可用 Skill。", file=repl.output)
        return True
    for item in entries:
        permission = "只读" if item.manifest.read_only else "可写（仍需逐次审批）"
        tools = "、".join(item.manifest.required_tools) or "无"
        print(
            f"{item.id} v{item.version}｜{permission}｜工具：{tools}｜{item.manifest.description}",
            file=repl.output,
        )
    return True


def skill(repl, argument: str) -> bool:
    """启用自动选择、显式选择或关闭 Skill；拒绝任何路径形式。"""
    arg = argument.strip()
    if not arg:
        raise ValueError("用法：/skill auto|<id>|off")
    if arg in {"auto", "off"}:
        repl.set_skill(arg)
        state = "已启用自动选择" if arg == "auto" else "已关闭"
        print(f"Skill {state}。", file=repl.output)
        return True
    if any(char in arg for char in _PATH_CHARS):
        raise ValueError("Skill 只能按内置 ID 选择，不接受文件路径")
    repl.set_skill("explicit", arg)
    selected = repl.skills.get(arg)
    print(f"Skill 已选择：{selected.id} v{selected.version}。", file=repl.output)
    return True
