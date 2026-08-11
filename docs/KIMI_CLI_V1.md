# Pi-Claw v1：Kimi CLI 路线的会话式 Coding Agent

## 定位

Pi-Claw v1 是一个使用 Python 3.12 和 uv 的学习型 Coding Agent。它参考 Kimi CLI 类工具的
核心工程路线：在终端中持续对话、记住项目上下文、按需调用工具、逐次审批副作用并恢复历史
会话。

这不是 Kimi Code 的完整复刻。项目没有宣称具备相同模型效果、全部命令、IDE/云端集成、远程
基础设施或生产级 Sandbox。真实 Kimi 模型只是可选的 OpenAI-compatible Provider；离线模式
仍能验证会话和安全内核。

## 从 v0 到 v1

```text
v0 安全内核
Workspace -> Policy -> Approval -> Tool -> ToolResult -> Trace

v1 会话层
System/Project Prompt -> Session -> Active Context -> Agent Loop
                                      -> v0 安全内核
```

- v0 回答“模型提出动作后，怎样限制、执行、记录”。
- v1 回答“多轮任务怎样继续、压缩、恢复，并保留原始证据”。
- v1 复用而不是绕过 v0；恢复会话后的工具调用仍需 Policy 和审批。

## 最小离线体验

```bash
uv sync --locked

uv run pi-claw chat \
  "只读检查当前项目入口文件" \
  --workspace "$PWD" \
  --print

uv run pi-claw sessions --workspace "$PWD"
uv run pi-claw chat --workspace "$PWD" --continue
```

`--print` 完成首条任务后退出，适合脚本和验收；省略它会进入交互 REPL。默认 `scripted` 使用
确定性离线 Provider，只验证 Session、Context、Tool 和 Trace，不声称理解或修改了代码。

指定会话恢复：

```bash
uv run pi-claw chat \
  --workspace "$PWD" \
  --session <SESSION_ID>
```

## REPL 命令

| 命令 | 作用 |
| --- | --- |
| `/new` | 创建新会话 |
| `/sessions` | 列出当前工作区会话 |
| `/resume <id>` | 恢复指定会话 |
| `/compact [关注点]` | 压缩活动上下文，保留原始历史 |
| `/context` | 显示消息数和近似字符/Token |
| `/history` | 打印当前活动消息摘要 |
| `/title <标题>` | 修改会话标题 |
| `/exit` | 退出并打印恢复命令 |

会话按工作区真实路径隔离。默认存放在 `~/.pi-claw/sessions`；测试或隔离运行可用
`--data-dir <PATH>`，也可用 `PI_CLAW_HOME` 改变默认根目录。

## 三种会话数据

```text
state.json           标题、时间、Provider/模型/Base URL、本会话批准 scope（不含 API key）
context.jsonl        append-only 原始消息历史
active_context.json  下一回合使用的有界活动窗口
```

`/compact` 只更新 `active_context.json`，不会覆盖 `context.jsonl`。摘要会保留系统消息和最近
消息，并把历史 Tool 输出标记成不可信数据。若极端巨大结构无法在不破坏 system/tool 协议的
前提下压缩，系统会明确失败而不是生成非法消息。加载原始/活动上下文都有消息数和字节上限。

默认会话目录和文件权限分别收紧为 `0700`、`0600`。恢复真实会话会沿用 Provider、模型和
Base URL；API key 永不写入 Session，仍须由当前终端环境提供。

## 系统与项目 Prompt

新会话的系统 Prompt 固定 `Inspect -> Plan -> Act -> Verify -> Report`，并强调工作区、证据、
安全和 uv 规则。项目上下文只发现工作区根目录的少量约定入口，如 `AGENTS.md`、README、
`pyproject.toml` 和 `说明.md`；每段都标注来源并受字符预算限制。

项目文件内容只是数据，不能覆盖系统安全约束。不要把项目 README 中的命令当作自动批准指令。

## Tool Call 与审批

Provider 可以在同一轮返回多个标准 `tool_calls`。Agent 把它们记录在一条 assistant 消息中，
随后按返回顺序执行并为每个调用追加 ToolResult。v1 不并行执行；存在依赖的动作应该分轮等待
上一结果。

只读工具默认允许，副作用进入三态审批：

- `y`：只允许这一次；
- `a`：本会话允许同一精确 scope；
- `n`：拒绝。

非 TTY 默认拒绝，不会在 CI 或管道中阻塞。`--yes` 自动批准 Policy 允许进入审批的动作，但
不能绕过路径越界、敏感文件和危险命令硬拒绝。

写入工具分工：

- `write_or_replace`：新建文件或完成一个唯一精确替换；
- `apply_patch`：对同一文件顺序执行 1 到 50 个唯一精确替换。所有 edit 先在内存校验，通过
  后一次写回；任一 `old` 不存在或不唯一时零写入。这不是抗进程崩溃的 OS 级原子替换承诺。

## uv 命令边界

v1 Coding Profile 允许少量可复现命令进入 ASK：

- `uv sync --locked` / `uv sync --frozen` / `uv lock --check`；
- 受限参数的 `uv run pytest`；
- `uv run ruff check` 和 `uv run ruff format --check`；
- `uv run python -m unittest` 与 `compileall`。

`uv add`、任意 `python -c`、Ruff `--fix`、外部路径和未知选项拒绝。即使被允许，测试仍会执行
仓库代码，sync 也可能访问包索引；这不是进程或网络 Sandbox。

## 真实模型

真实模型仍使用：

```bash
uv run pi-claw chat \
  "检查项目并给出下一步" \
  --workspace "$PWD" \
  --provider openai \
  --print
```

这里的 `--provider openai` 表示 OpenAI-compatible Chat Completions 适配器，不等于只能使用
OpenAI。OpenAI、Kimi、Qwen/阿里云百炼和 DeepSeek 的 Key、Base URL、模型映射与兼容性检查
见 [API Key 指南](./API_KEY_GUIDE.md)。

## v1 验收清单

- 离线 `chat --print` 能创建 Session 并生成 Trace。
- `sessions`、`--continue`、`--session` 能恢复同一工作区会话。
- Context 压缩后活动窗口变小，原始 JSONL 不变。
- 允许一次不会持久化，本会话允许会保存精确 scope，非 TTY 默认拒绝。
- 同轮最多 16 个 Tool Call，均得到独立 ToolResult；超过上限安全失败且不执行。
- `apply_patch` 任一 edit 不唯一时文件完全不变。
- uv 安全形态返回 ASK，包变更、任意代码和路径逃逸返回 DENY。
