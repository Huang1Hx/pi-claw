"""只读运行状态，无需模型 API。"""

import json

from commands.model import current_model
from config.models import SandboxSettings


def _sandbox_status(repl) -> tuple[str, str]:
    """报告实际 Runner；Host 不得冒充已启用 Docker 资源隔离。"""
    runner = repl.command_runner
    configured = repl.settings.sandbox if repl.settings else SandboxSettings()
    backend = str(getattr(runner, "backend", configured.backend))
    if backend != "docker":
        return backend, "未启用（可信工作区兼容模式）"
    limits = getattr(runner, "settings", configured)
    return (
        backend,
        f"{limits.memory_mib} MiB、{limits.cpus} CPU、{limits.pids} PIDs、网络 {limits.network}",
    )


def status(repl, argument: str) -> bool:
    """默认显示日常所需信息；原始字段通过 --verbose 保留，绝不混淆消息数和 Token。"""
    if argument not in {"", "--verbose"}:
        raise ValueError("用法：/status [--verbose]")
    backend, resource_limits = _sandbox_status(repl)
    if not argument:
        labels = {
            "completed": "已完成（通过宿主验收）",
            "unverified": "未通过完成验收",
            "provider_error": "模型请求失败",
            "max_steps": "达到步骤上限",
            "loop_detected": "检测到重复调用",
            "failed": "执行失败",
        }
        result = (
            labels.get(repl.last_result.status.value, repl.last_result.status.value)
            if repl.last_result
            else "本次会话载入后尚未执行任务"
        )
        approval = "自动批准（仍受策略限制）" if repl.auto_approve else "按需确认副作用"
        scopes = len(repl.session.approved_tools)
        if scopes:
            approval += f"；当前会话已保存 {scopes} 个授权范围"
        print(
            f"工作区：{repl.workspace}\n会话：{repl.session.session_id}\n"
            f"模型：{current_model(repl)}\n审批：{approval}\n"
            f"执行后端：{backend}\n资源限制：{resource_limits}\n"
            f"最近结果：{result}\n"
            "Token 用量见 /usage；上下文见 /context；调试详情见 /status --verbose。",
            file=repl.output,
        )
        return True
    value = {
        "workspace": str(repl.workspace),
        "session": repl.session.session_id,
        "provider": repl.provider_name,
        "model": repl.model,
        "messages": len(repl.messages),
        "auto_approve": repl.auto_approve,
        "last_status": repl.last_result.status.value if repl.last_result else None,
        "routing": repl.last_result.routing if repl.last_result else None,
        "sandbox": vars(repl.settings.sandbox if repl.settings else SandboxSettings()),
    }
    print(json.dumps(value, ensure_ascii=False, indent=2), file=repl.output)
    return True
