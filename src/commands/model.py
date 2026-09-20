"""查看或显式选择已配置模型，切换时建立新的隔离会话。"""

from collections import defaultdict

from config import Settings


def model_profiles(settings: Settings | None) -> dict[str, list[str]]:
    """只提取模型 ID 与配置别名，不将连接地址、密钥或整个配置对象交给界面。"""
    names = defaultdict(list)
    if settings:
        for name, profile in settings.profiles.items():
            names[profile.model].append(name)
    return dict(names)


def resolve_model_selection(name: str, settings: Settings | None) -> str:
    """具体模型 ID 唯一时映射到已有 profile；重名必须选别名，保留参数和连接边界。"""
    if name in {"auto", "scripted"}:
        return name
    if not settings:
        raise ValueError("请通过 --config 加载模型配置")
    # 已有别名优先，兼容 /model primary；不会根据未配置 ID 拼装新的 API 请求。
    if name in settings.profiles:
        return name
    matches = model_profiles(settings).get(name, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"同一模型对应多个配置，请使用别名选择：{', '.join(matches)}")
    raise ValueError("未知模型 ID 或配置别名，请输入 /model 查看")


def model_options(settings: Settings | None) -> dict[str, str]:
    """生成实际 ID 优先的补全菜单；别名带上模型名，重名 ID 不提供歧义快捷入口。"""
    options = {"scripted": "离线演示，不调用真实模型"}
    if not settings or not settings.profiles:
        return options
    options["auto"] = "自动路由；按配置选择模型"
    for model_id, names in model_profiles(settings).items():
        if len(names) == 1 and model_id not in {"auto", "scripted", *settings.profiles}:
            options[model_id] = f"固定模型 · {names[0]} · 切换后新会话"
    for name, profile in settings.profiles.items():
        options[name] = f"{profile.model} · 配置别名 · 切换后新会话"
    return options


def current_model(repl) -> str:
    """显示当前运行模式；auto 不是模型 ID，不把上一回合的模型当下一回合的固定选择。"""
    if repl.provider_name == "scripted":
        return "scripted（离线演示）"
    if repl.provider_name == "routed" and repl.model == "auto":
        return "自动路由（具体模型见 /model）"
    profile = repl.settings.profiles.get(repl.model) if repl.settings else None
    if repl.provider_name == "routed" and profile:
        return f"{profile.model}（固定模型；配置别名 {repl.model}）"
    return repl.model or "未指定模型"


def model(repl, argument: str) -> bool:
    """无参数展示实际模型与自动路由分配；选择操作由 REPL 统一解析并新建会话。"""
    if not argument:
        print(f"当前：{current_model(repl)}", file=repl.output)
        profiles = model_profiles(repl.settings)
        if profiles:
            print("已配置模型（模型 ID → 配置别名）：", file=repl.output)
            for model_id, names in profiles.items():
                print(f"  {model_id} → {', '.join(names)}", file=repl.output)
            print(
                "选择：/model <模型ID>；别名兼容；同一 ID 有多个配置时请选别名。", file=repl.output
            )
            rules, flow = repl.settings.routing, repl.settings.workflow
            roles = [("轻量/分类", rules.cheap), ("主力", rules.strong)]
            if flow.enabled:
                roles += [("规划", flow.planner), ("编码", flow.coder), ("审查", flow.reviewer)]
            print("auto 的配置分工（不是可用性测试结果）：", file=repl.output)
            for label, name in roles:
                profile = repl.settings.profiles.get(name)
                print(
                    f"  {label}：{profile.model if profile else '未配置'} [{name}]",
                    file=repl.output,
                )
            print("/model auto 启用自动路由；固定模型会跳过独立规划/审查。", file=repl.output)
        else:
            print("未加载模型列表，请通过 --config 指定配置。", file=repl.output)
        print("/model scripted 使用离线演示。切换会创建新会话，旧会话保留。", file=repl.output)
        return True
    repl.select_model(argument)
    return True
