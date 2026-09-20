"""终端会话控制命令；不进入 LLM 消息，也不执行 shell。"""

import shlex


def new_session(repl, argument: str) -> bool:
    """创建独立会话。"""
    repl.session = repl._create_session()
    repl.messages, repl._session_seq = repl.store.load_active_snapshot(
        repl.session.session_id,
        repl.workspace,
        repair_incomplete=True,
    )
    repl.store.save_active_messages(
        repl.session.session_id,
        repl.workspace,
        repl.messages,
        expected_seq=repl._session_seq,
    )
    repl._refresh_recovery_status()
    repl._build_runtime()
    repl.last_result = None
    print(f"已创建新会话：{repl.session.session_id}", file=repl.output)
    return True


def title(repl, argument: str) -> bool:
    """设置会话标题。"""
    repl.session = repl.store.update_state(
        repl.session.session_id,
        repl.workspace,
        title=argument,
    )
    print(f"会话标题已更新：{repl.session.title}", file=repl.output)
    return True


def resume(repl, argument: str) -> bool:
    """恢复同配置会话，跨模型恢复需显式重启。"""
    if not argument:
        repl._print_sessions()
        return True
    candidate = repl.store.load(argument, repl.workspace)
    if (
        candidate.provider != repl.provider_name
        or candidate.model != repl.model
        or candidate.base_url != repl.base_url
    ):
        command_parts = [
            "uv run pi-claw chat",
            f"--workspace {shlex.quote(str(repl.workspace))}",
            f"--session {shlex.quote(candidate.session_id)}",
            f"--provider {shlex.quote(candidate.provider)}",
        ]
        if candidate.model:
            command_parts.append(f"--model {shlex.quote(candidate.model)}")
        if candidate.base_url:
            command_parts.append(f"--base-url {shlex.quote(candidate.base_url)}")
        if candidate.provider == "routed":
            config = (
                str(repl.settings.config_path)
                if repl.settings and repl.settings.config_path
                else "/ABS/PATH/pi-claw.toml"
            )
            command_parts.append(f"--config {shlex.quote(config)}")
        print(
            "目标会话使用不同的 Provider 运行时。为避免静默混用模型，"
            "请退出后运行：\n" + " ".join(command_parts),
            file=repl.output,
        )
        return True
    repl.session = candidate
    repl.messages, repl._session_seq = repl.store.load_active_snapshot(
        repl.session.session_id,
        repl.workspace,
        repair_incomplete=True,
    )
    repl.store.save_active_messages(
        repl.session.session_id,
        repl.workspace,
        repl.messages,
        expected_seq=repl._session_seq,
    )
    repl._refresh_recovery_status()
    repl._build_runtime()
    repl.last_result = None
    print(f"已恢复会话：{repl.session.session_id}", file=repl.output)
    return True


def sessions(repl, argument: str) -> bool:
    """列出当前工作区会话。"""
    repl._print_sessions()
    return True
