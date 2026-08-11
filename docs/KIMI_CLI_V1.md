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

`/resume` 还会比较目标会话的 Provider、模型和 Base URL。若任一项与当前 REPL 不同，它不会
静默把当前模型换掉，而会要求退出并打印类似下面的重启命令：

```bash
uv run pi-claw chat \
  --workspace "/原工作区" \
  --session "<SESSION_ID>" \
  --provider "<PROVIDER>" \
  --model "<MODEL>" \
  --base-url "<BASE_URL>"
```

按实际提示执行；没有模型或自定义 Base URL 时，相应参数不会出现。API key 不在命令中，仍由
当前终端环境提供。

## 三种会话数据

```text
state.json                    标题、时间、Provider/模型/Base URL、会话批准 scope
context.jsonl                 第一段 append-only 消息 journal
context-<start_seq>.jsonl     超过分段阈值后的后续 journal
active_context.json           {version, through_seq, messages} 活动上下文 checkpoint
```

journal 每个新格式行都包含严格递增的 `seq` 和一条 `message`。当当前段超过阈值，或检测到末尾
存在未完整写入的行时，后续消息写入 `context-<start_seq>.jsonl`；文件名中的 start_seq 是该段
第一条消息的序号。分段改变存储文件，不改变消息顺序或“原始历史不被压缩覆盖”的原则。

`/compact` 把有界消息窗口保存为 checkpoint：`through_seq` 表示窗口已经吸收到了哪条 journal
记录。恢复时先读取 checkpoint，再只重放 `seq > through_seq` 的 journal 尾部，因此 checkpoint
保存后刚追加的消息不会丢失，也不会重复。最后一条没有换行的半写记录会被忽略；已经换行却
不是合法 JSON 的损坏记录会明确报错，而不是悄悄跳过。

如果进程在 assistant `tool_calls` 已落盘、对应 ToolResult 尚未落盘时中断，恢复会为缺失结果补
一个 `synthetic_recovery` 失败结果。系统不会猜测工具是否已经产生副作用，也不会自动重放；
用户应先检查工作区和 trace，再决定是否重新发起动作。

摘要会保留系统消息和最近消息，并把历史 Tool 输出标记成不可信数据。若极端巨大结构无法在
不破坏 system/tool 协议的前提下压缩，系统会明确失败而不是生成非法消息。加载原始/活动上下文
都有消息数和字节上限；checkpoint 与恢复时追加的 journal 尾部共用同一字节预算。

会话 append 会 `flush + fsync`；`state.json` 和 `active_context.json` 通过同目录临时文件完整
落盘后 `os.replace`，并同步父目录。默认会话目录和文件权限分别为 `0700`、`0600`。这些措施
降低进程中断留下半写状态的概率，不是数据库事务，也不承诺所有文件系统上的绝对断电安全。
同一 Session 的提交由文件锁跨进程串行化；REPL 保存时还校验已加载的 journal 序号，发现另一
实例已推进会话就拒绝 stale 写入并要求重新加载。
恢复真实会话会沿用 Provider、模型和 Base URL；API key 永不写入 Session，仍须由当前终端
环境提供。

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

真正让用户确认的不是只有工具名：

- 写入展示有长度上限、已脱敏的 unified diff；超长预览会明确标记截断；
- 命令展示完整 argv 数组和 cwd，不把复杂命令折叠成模糊前缀；
- “本会话允许”对写入绑定完整调用参数与已审阅文件的 preimage 哈希，对命令绑定完整 argv 与
  cwd；参数或文件版本变化后需重新审批；
- 写入在生成预览时记录目标哈希，批准后执行前再次计算。若文件期间被其他进程改动，拒绝写入并
  要求重新预览、重新批准，降低审批对象与实际写入对象不一致的 TOCTOU 风险。

写入工具分工：

- `write_or_replace`：新建文件或完成一个唯一精确替换；
- `apply_patch`：对同一文件顺序执行 1 到 50 个唯一精确替换。所有 edit 先在内存校验，通过
  后一次写回；任一 `old` 不存在或不唯一时零写入。这不是抗进程崩溃的 OS 级原子替换承诺。

文件内容先写入目标同目录的临时文件并 `fsync`。替换已有文件使用 `os.replace`，并保留原文件
权限；新建文件使用硬链接独占创建，若目标已出现则不会覆盖。临时新文件默认只有当前用户可
读写（`0600`），父目录也会同步。它们是应用层写入加固，不是对恶意进程的 OS Sandbox。

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

Provider 成功响应与 HTTP 错误响应都有读取字节上限，默认最多 `5,000,000` 字节，避免异常服务
返回无界响应体。Session 保存 Base URL 前也会校验：必须是带主机名的 HTTP(S) 根地址，且不得
包含用户名、密码、query 或 fragment。两项都是输入边界，不代表网络隔离或第三方服务可信。

## v1 验收清单

- 离线 `chat --print` 能创建 Session 并生成 Trace。
- `sessions`、`--continue`、`--session` 能恢复同一工作区会话。
- Context 压缩后活动窗口变小，原始 JSONL 不变。
- checkpoint 后新增 journal 能在恢复时重放；最后半行会忽略，中断 Tool Call 会补失败结果且不重放。
- 允许一次不会持久化，本会话允许会保存精确 scope，非 TTY 默认拒绝。
- 同轮最多 16 个 Tool Call，均得到独立 ToolResult；超过上限安全失败且不执行。
- `apply_patch` 任一 edit 不唯一时文件完全不变。
- uv 安全形态返回 ASK，包变更、任意代码和路径逃逸返回 DENY。
