# 多模型工作流：规划 → 编码 → 常规审查 → 可选高级审查

该功能在单个 Agent 宿主中按顺序协调多个模型，不是分布式 Multi-Agent，也不启动并行子代理。
阶段之间自动切换，写文件和运行命令仍需原有审批。默认旧配置关闭工作流，学校模板显式开启。

## 模型分工与启动

| 职责 | profile | 学校网关 model ID | 权限 |
|---|---|---|---|
| 复杂度分类、简单任务 | economy | `deepseek-flash`（学校） | 分类无工具；简单执行只读 |
| 规划 | planner | `deepseek-flash`（学校） | 读取、枚举、搜索 |
| 编码和验证 | primary | `deepseek-flash` | 原工具集合；副作用仍须审批 |
| 常规独立审查 | reviewer | `deepseek-flash`（学校） | 读取、枚举、搜索 |
| 高级独立审查 | premium_reviewer | `kimi-k3`（Moonshot） | 仅命中风险规则；读取、枚举、搜索 |

普通自动角色全部使用学校免费的 `deepseek-flash`；多个 profile 映射同一个 model ID，但仍保留
阶段权限、输出上限和调用统计。用户自费 `kimi-k3` 只做必要的第二次只读审查，不参与分类、规划
或编码。该分配不是通用能力排行榜，也不根据价格自动排序。
模型 ID 依据 2026-09-14 查询的[学校公开模型目录](https://llm.ustc.edu.cn/api/models/public)，
接入约定见[学校 API 指南](https://llm.ustc.edu.cn/guide/api-usage/)和
[协议说明](https://llm.ustc.edu.cn/guide/protocol/)。服务目录与能力可能变化；真实兼容性仍需联调。

新克隆可将根目录 `pi-claw.ustc.example.toml` 复制为 `pi-claw.toml`。
**已经填写本机配置的用户不要覆盖现有文件。** `.env` 中学校连接使用
`OPENAI_API_KEY`/`OPENAI_BASE_URL`，Kimi 使用 `MOONSHOT_API_KEY`/`MOONSHOT_BASE_URL`；
不需要给学校的四个角色重复配置 Key，也不需要价格。
模型 ID 由 TOML 加载，此模式不使用 `.env` 的 `OPENAI_MODEL`。

从项目根目录启动（会产生真实模型调用）：

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route auto --max-steps 24
```

随后输入具体编码任务，例如“修复某个函数的边界错误，补充测试并验证”。简单问答仍走 economy；
复杂任务、分类失败、轻量执行越界或失败升级后进入完整工作流。终端显示 `workflow>` 阶段，
`review>` 显示审查结论；`/status --verbose` 查看本回合阶段、请求分布和 Token，
`/status` 显示简明状态，`/usage` 查看当前会话与项目工作区累计 Token。
`--route primary` 固定编码模型，**绕过分类和工作流**；备选 profile 也只在显式选择时使用。
`--no-stream` 仅关闭编码 SSE，可用于排查流协议问题。未配置工作流时保留原两档路由行为。

当前默认工作区为启动目录的 `projects/`，不再默认使用宿主源码根目录。要恢复旧根目录会话
或修改 Pi-Claw 本身，请显式 `--workspace .`。代码示例的新建/保存请求即使实现很短，也需
进入有写工具的分支；宿主补充识别“写一个”“新建”“创建”“保存”等表达，仍属于保守启发式。

## 真正执行的状态转换

```text
用户任务 → economy 分类 → 简单：economy 只读执行
                       → 复杂/失败：planner 规划 JSON
                                      ↓
                                  primary 编码/验证 ← 宿主拒绝验证 / 审查问题清单
                                      ↓                         ↑
                               写后最低验证证据检查              │
                                      ↓                         │
                                reviewer 只读审查 ── 要求修改 ──┘
                                      ↓ 批准且读过所有改动文件
                              宿主风险规则 ── 不触发 ───────────┐
                                      ↓ 触发                   │
                           premium_reviewer 重新只读审查        │
                                      ↓ 批准                   │
                                外层宿主最终验收 ←──────────────┘
                                      ↓
                                  completed
```

1. `RoutedProvider.begin_run()` 为每个新用户回合重置分类、预算和工作流状态。
2. `WorkflowCoordinator` 建立规划私有对话，模型可以先调用只读工具，再返回
   `{"summary":"...","steps":["..."],"verification":["..."]}`。
3. 宿主严格校验字段、长度和类型，拒绝重复字段、Markdown 包裹、空计划或任意权限字段。
   合法计划形成 `workflow_artifact`，写入 Session 和 Trace 后进入编码。
4. 编码模型得到任务、计划和历史证据预览，工具调用仍交 Agent → Hook → ToolExecutor →
   Policy/Approval → Workspace；协调器本身不执行文件操作或命令。
5. Agent 把真实 ToolResult 同时写入公共消息历史、会话和当前阶段私有对话。
   `ToolEvidenceCompletionVerifier` 要求最近成功写入之后出现至少一项认可的成功检查。
   编码模型宣称“测试通过”不能代替真实工具结果。证据不足时反馈编码模型继续处理。
6. 审查模型使用新对话；得到计划、编码说明、改动文件和宿主验证摘要。必须通过 `read_file`
   在**本次审查轮**完整重读所有成功改动的文件。缺失、失败或截断读取都不能批准。
7. 审查必须返回 `{"verdict":"approve","summary":"...","issues":[]}`，或
   `changes_requested` 并提供非空问题清单。否决带反馈回编码；再次改动须重新验证和审查。
8. 常规审查通过后，宿主仅按显式要求、高风险任务/路径、至少两轮常规审查或至少五个改动文件
   决定是否升级；没有成功改动文件时不调用 Kimi，不额外花模型调用做分类。
9. 高级审查建立新的私有对话，必须重新读取所有改动文件。Kimi 否决、越权、超时或预算耗尽
   均停止为未验证，不自动回退成常规审查通过，也不自动花费第二轮高级返工。
10. 最终审查通过只产生 `review_approved` 或 `premium_review_approved`。Agent 的最终
    CompletionVerifier 仍有否决权，`finish_run()` 仅在宿主接受后标记 `completed`。

规划、常规审查和高级审查发送的工具 Schema 只含只读工具，宿主还复核返回调用名称；即使用户
曾批准写入，这些阶段返回写入、命令或未知工具也会被拦截，整组不执行。它们的 JSON 不能选 profile，
模型和阶段映射只能来自配置。

## 消息、推理内容与持久化

- 公共历史保存用户任务、实际 assistant/tool 调用配对、结构化计划、编码候选、审查和最终答复。
  阶段产物作为带 `type=workflow_artifact` 的数据消息，不作为系统指令或用户新增授权。
- 每个阶段单独保存 `phase_history`。工具响应继续发给同一阶段模型；切换阶段重新建立对话，
  不把其他模型的 assistant/tool 协议状态作为新模型的原生对话续接。
- 跨阶段交接使用明确标注为不可信的 JSON 证据。历史最多预览 24 条非系统候选记录，每条最多
  6,000 字符并标明截断；计划、反馈、改动文件和宿主验证另外传递。截断信息需要模型重新读取。
- `reasoning_content` 在当前阶段私有内存中保留，以满足推理模型工具续接。工作流不把这些原始
  推理写入 Session、Trace、最终文本或其他阶段。SSE 思考片段只发出字符数进度，不显示思考正文。
- 普通单模型路径使用 `ModelReply.to_message()` 保存必要续接字段；内部 `_pi_claw_profile`
  不发送给服务，跨 profile 去掉私有推理。这不等于公开工作流的推理日志功能。
- Session 仍使用本机文件锁和 `expected_seq` 保护日志提交；不在等模型期间持锁，不用锁授权工具。
  每个 Agent 实例只运行一个回合。阶段隔离是对话/权限隔离，不是进程或容器隔离。
- 恢复会话能读到已落盘产物和工具结果，但**没有阶段状态机的精确断点续跑**。下一用户回合
  重新规划；丢失的私有推理不恢复，也不自动重放已执行工具。

## 参数与资源上限

学校模板中 `[workflow]` 为 `enabled=true`，普通阶段和可选高级审查映射如上表。

| 参数 | 学校模板值 | 含义 |
|---|---:|---|
| `routing.max_requests` | 36 | 每回合全部逻辑模型请求，含分类、产物纠错、失败和返工 |
| `max_planning_requests` | 4 | 规划阶段模型请求上限，工具请求也占用 |
| `max_review_requests` | 4 | 每轮审查请求上限，读取请求和协议纠错也占用 |
| `max_premium_review_requests` | 4 | 唯一一次高级审查阶段的请求上限，不是四轮复审 |
| `max_review_rounds` | 3 | 最多三轮审查，即首次审查加最多两次审查返工 |
| `max_context_chars` | 120000 | 当前阶段私有消息 JSON 字符上限，超限停止 |
| `--max-steps` | 建议 24 | 外层 Agent 回合上限，与逻辑请求上限独立 |
| `--max-attempts` | 默认 3 | 单个逻辑请求包含首次在内的 HTTP 尝试上限 |

规划/审查限额不是独立赠送的调用额度；全部还受总请求上限约束。外层一步可能在内部消费
计划、编码完成说明及审查产物，所以 `steps` 不等于模型请求数。学校 profile 默认最多三次 HTTP
尝试；Kimi profile 固定一次，不能用 `36 × 3` 推算实际付费次数。流中断后不重试或重放工具。

每个 `[models.*]` 可覆盖 `max_output_tokens`（1–32768）和 `timeout_seconds`（1–300）。
学校模板为 economy 2048 / 120 秒，规划/编码/常规审查 8192 / 120 秒，Kimi 4096 / 120 秒；
分类另收紧为最多 128 Token。
这是服务端 `max_tokens` 和客户端 HTTP 读取/连接超时配置，不是整个回合的墙钟时限，
也不是模型上下文窗口的保证值。

推理开关支持 `thinking_mode="default|enabled|disabled"`；default 不发送额外字段。
`thinking_format` 只允许 `thinking`、`chat_template_kwargs`、`enable_thinking` 三种受控形式。
学校模板只给 `primary` profile 发送 `thinking.type=enabled`；同为 DeepSeek 的其他普通角色保持
服务默认值，Kimi 也不附加学校网关专用推理字段。
参数处理已用替身测试，学校网关
是否接受、忽略或转换这些字段尚未实测；遇到相关 400 错误应核对平台协议，不盲目重试。

## 指标与边界

`RunResult.routing.requests_by_purpose` 按 classification/execution/planning/coding/reviewing/
premium_reviewing
区分逻辑调用，`models` 按 profile 聚合服务端 Token。中间响应和被拒绝的产物也计入 usage，
最后的审查响应只计一次；失败请求的未知 Token 不猜测为零，不推算未知费用。

`RunResult.routing.workflow` 还记录 premium_review_triggered、premium_review_reason、
premium_review 和 premium_reviewed_files。阶段协议、权限、证据或审查上限失败为
`unverified`；网络/模型全局预算错误仍为 `provider_error`；重复工具或外层步数耗尽沿用原终态。
重复检测按阶段重建，允许不同职责各自读取相同文件；同阶段循环仍停止，跨阶段返工由预算约束。

工作流不会自动提交 Git、回滚文件或保证任务语义正确。审查前的写入已经落地；审查失败时
保留改动供用户检查。文件重读是时点证据，不锁住文件以防其他进程随后修改。大文件若无法在
读取预算内完整返回，会阻止批准。认可的检查仍是最低证据，不证明所运行的测试足以覆盖任务。
真实模型质量、工具兼容性、延迟与权限需要独立验证，离线通过率不是生产任务成功率。
