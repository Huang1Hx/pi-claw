# Pi-Claw 项目边界与验收基线

## 一句话目标

Pi-Claw 是一个使用 Python/uv、沿 Kimi CLI 路线学习会话式 Coding Agent 的项目：它在指定
工作区内完成 `Inspect -> Plan -> Act -> Verify -> Report`，所有工具调用都经过权限策略并
写入可回放轨迹。

## 演进路线

- **v0 安全内核**：Workspace、Policy、Tool、Trace、Provider 和有最大步数的 Agent Loop。
- **v1 会话式 Agent**：Session、Active Context、项目 Prompt、三态审批、多 Tool Call、
  `chat --print`、交互 REPL 和 `sessions`。
- **v1.1 离线评测基线**：版本化离线 case、证据断言、每 case Trace 与机器可读 JSON 指标。
- **v1.2 本地并发评测与架构说明**：独立 Workspace 并发、隔离指标、消息流/锁/分布式设计边界。
- **v1.3 可验证完成**：写入后检查真实验证 ToolResult，拒绝无证据的完成声明。

v1 参考 Kimi CLI 类产品的工程路线，但不复刻完整 Kimi Code，不宣称相同模型能力、全部命令、
远程基础设施、IDE 集成或生产级 Sandbox。

## 当前 v1 范围

- 支持小型本地 Python 仓库。
- 提供文件枚举、文本搜索、文件读取、精确替换、全量校验后一次写回的 `apply_patch` 和受限命令工具。
- 默认只读；修改文件或执行命令需要明确批准。
- 不需要网络、模型 API 或 Docker，也能运行固定脚本模型演示与测试。
- 可创建、列出、加载和恢复按工作区隔离的本地会话。
- 原始消息以带递增 `seq` 的分段 journal 保存；压缩只更新带 `through_seq` 的活动 checkpoint，
  不覆盖原始历史。
- 交互审批支持允许一次、本会话允许同类和拒绝，非 TTY 默认拒绝。
- 审批展示有界脱敏 diff、目标 preimage 或完整 argv/cwd；写 session scope 绑定参数与已审阅版本，
  写入执行前再次复核目标哈希。
- Provider 可返回同轮多个 Tool Call，Agent 按顺序执行并分别记录结果。
- 可选接入 OpenAI-compatible Chat Completions API，包括 Kimi、Qwen、DeepSeek 和 OpenAI。
- OpenAI-compatible 响应体有字节上限；会话 Base URL 元数据拒绝内嵌凭据、query 和 fragment。
- JSONL 轨迹包含步骤、工具、结果和 UTC 时间戳，并对常见凭据字段脱敏。
- Agent 受最大步数限制，不允许无限循环。
- `eval` 复用真实 Agent/Policy/Tool/Trace 链路，覆盖修复、失败恢复、安全拒绝和熔断场景。
- `eval --workers N` 可在本机并行运行相互独立的 case，并量化最大并发、墙钟时间、观察加速比和
  任务隔离违规；不共享 Agent 可变状态。
- CLI、REPL、Demo 和 Eval 启用宿主完成验收；写入后没有成功验证证据时不会返回 `completed`。

## 明确不做

- 不做 Web UI、IDE 插件、跨仓库自治和联网搜索。
- 不执行 `sudo`、`curl/wget`、任意包变更、`git push/reset/clean` 等高风险操作。
- Coding Profile 只允许少量可复现的 `uv` 同步/检查/测试形式，并始终需要审批。
- 不自动提交或推送 Git 变更。
- 不安装 Docker；容器 Sandbox 仅作为后续可选增强。
- 不为了展示而强行加入 Multi-Agent；先建立可测的单 Agent 基线。
- 不宣称已实现跨机器分布式调度、分布式锁、远程队列或生产级容错；当前只有本机并发 Eval。
- 不宣称最低验证证据等于完整业务正确性；无写入自然语言事实和任务级验收仍是后续范围。

## 权限模型

1. 所有路径先解析为真实路径，并必须位于工作区根目录内。
2. 只读工具可自动通过；写工具返回 `ask`，由 CLI 或调用方批准。
3. 命令以 argv 数组执行，禁止 `shell=True`；可执行程序必须在白名单内。
4. `.env`、`.git` 等敏感路径不得读取或修改。
5. 网络默认关闭；工具不能自行扩大权限。

## v1 验收

- `uv sync` 后不设置 API key，也能运行离线演示。
- 单元、集成、安全测试全部通过。
- 路径穿越、符号链接越界、敏感文件访问和危险命令会被拒绝。
- 文件写入未经批准不会发生，批准后只修改目标文件。
- `apply_patch` 任一 edit 不存在或不唯一时，整个文件保持原样。
- `chat --print` 可离线创建会话；`sessions`、`--continue`、`--session` 可恢复。
- Context 压缩后活动窗口变小，append-only 原始消息仍完整保留。
- checkpoint 后的 journal 尾部可恢复；最后半行会忽略，中断 Tool Call 补失败结果且不自动重放。
- 非 TTY 副作用审批默认拒绝，本会话批准只作用于精确工具 scope。
- 会话 append/state/checkpoint 和工具文件写入使用 `fsync`；已有文件替换与新文件独占创建均先
  落盘内容，再让完整文件可见。
- REPL `/resume` 遇到 Provider、模型或 Base URL 不一致时拒绝静默切换，并打印显式重启命令。
- 每次 CLI 运行默认生成脱敏 JSONL 轨迹；库调用方也可显式传入 TraceWriter。
  回放只读取历史事件，不重新执行工具。
- README 能让初学者在 15 分钟内理解架构并完成演示。
- 默认 Eval 可离线运行；篡改期望、未知 case 或非法 fixture 路径时必须报错而不是误报通过。
- 成功写入后无验证证据的 final candidate 必须进入 `completion_rejected`；步数耗尽返回
  `unverified`，成功验证后才允许 `completed`。
