# Iteration 003：工作流梳理与可验证完成

## 状态

`已提交 optimize`。2026-09-10 用户授权提交，随 Iteration 001/002 一并提交至本地 `optimize`；未合并 `main`、未 push。

## 基线

Iteration 002 后，Pi-Claw 已具备 Session、Context、Policy/Approval、受限 Tools、Trace 和离线
并发 Eval，但 `Agent.run()` 遇到模型 `final_text` 会直接返回 `completed`。模型可能在没有运行
测试或测试失败后错误声明完成。

## 目标

1. 用源码路径完整说明 CLI、消息、工具、权限、Session、Trace 和 Eval 数据流；
2. 对照公开 Coding Agent 项目，选择适合轻量架构的增强点；
3. 在不扩大权限的前提下增加宿主侧完成验收；
4. 用离线 case 证明未验证完成不会被记录为成功。

## 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/pi_claw/completion.py` | 新增 | CompletionVerifier 协议、工具观察和最低验证证据规则 |
| `src/pi_claw/agent.py` | 修改 | final candidate 验收、拒绝反馈、`unverified` 终态和 Trace 事件 |
| `src/pi_claw/schemas.py` | 修改 | `RunStatus.UNVERIFIED` 与 RunResult 验收数据 |
| `src/pi_claw/repl.py`、`cli.py` | 修改 | 产品入口启用宿主完成验收 |
| `src/pi_claw/eval.py`、`eval_cases.json` | 修改 | suite v3、完成验收指标与虚假完成防护 case |
| `tests/test_completion.py` | 新增 | 认可命令、顺序约束、失败验证和只读完成测试 |
| `tests/test_agent.py` | 修改 | 拒绝后恢复、无证据返回 unverified 的循环测试 |
| `docs/WORKFLOW_AND_DATA_FLOW.md` | 新增 | 端到端工作流和存储路径 |
| `docs/UPSTREAM_CODING_AGENT_COMPARISON.md` | 新增 | 官方项目能力对照与采用边界 |

## 设计取舍

- 验收器不自行执行命令，只消费已通过 Policy/Approval 的真实 ToolResult，因此不扩大权限。
- 只统计最后一次成功写入之后的检查，避免把“修改前测试通过”误当成修改后证据。
- 当前接受 test、lint、compile、lock 四类最低证据；这不是业务正确性的完整证明。
- 被拒绝的 final candidate 不进入 assistant 历史；Trace 记录拒绝，宿主反馈进入下一轮。
- 步数耗尽时，REPL 会以 `candidate(unverified)` 显示候选文本，避免伪装成已验收回复。
- Agent 类默认仍允许调用方不传 verifier，以保留嵌入式 API 兼容；CLI、REPL、Demo 和 Eval 默认启用。

## 验收命令

```bash
uv sync --locked --default-index https://pypi.org/simple --no-config
uv lock --check --default-index https://pypi.org/simple --no-config
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen python -m compileall -q src/pi_claw
uv run --frozen pytest --cov=pi_claw --cov-report=term-missing
uv run --frozen pi-claw eval --workers 4
uv build --no-config
git diff --check
```

## 测试证据

- 全量测试：`203 passed`；
- 覆盖率：总计 `77%`，其中 `completion.py 82%`、`agent.py 92%`、`eval.py 86%`；
- 离线 Eval：`8/8` case 通过，`14` 次工具调用，安全断言 `100%`；
- 完成验收：`1` 次虚假完成被拒绝，`1` 个 run 返回 `unverified`，`6` 个完成状态均有验收记录；
- 并发 Eval：请求/实际 worker `4/4`，最大并发 `4`，隔离违规 `0`；
- Ruff、compileall、`uv lock --check`、`uv build` 和 `git diff --check` 均通过。

## 技术价值

完成状态从“相信模型文本”变成“检查宿主工具证据”。这降低了写入后未测试却误报成功的风险，
并把拒绝、恢复和未验证终态纳入 Trace 与 Eval。

## 遗留边界

- 无写入的自然语言事实仍未做语义验真；
- 写入失败或被拒后，若模型声称成功，目前也没有自然语言级真实性分类；
- 任意一个认可检查成功只是最低证据，尚未绑定任务指定测试；
- Repository Map、插件系统、真实 Subagent 和 Docker Sandbox 均未实现；
- 真实模型完成真实性、成本和延迟尚未形成重复实验矩阵。
