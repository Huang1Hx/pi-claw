# Pi-Claw 架构

## 依赖方向

```text
interfaces → commands / config
           → core → providers / routing → providers
                  → tools → security
                  → persistence
evaluation → 正式 core / routing / tools / security / persistence
```

core.schemas、core.events、core.usage 是低层数据协议。providers 和 security 可以依赖这些
数据类型，不反向依赖 Agent。Runtime 不导入 evaluation，评测命令按需加载 evaluation。
上述模块直接对应 `src/core/`、`src/interfaces/` 等目录；`src/` 是源码根，不是 Python 包。

## 模块职责

| 模块 | 核心文件 | 职责 |
|---|---|---|
| core | agent.py、team.py | 单 Agent 循环；只读子 Agent 并行、交接和唯一写者协调 |
| core | context.py、completion.py | 活动窗口控制、最低验证证据检查 |
| core | events.py、usage.py、loop_detection.py、hooks.py | 事件协议、实际用量、重复检测、扩展回调 |
| tools | executor.py | 唯一模型工具执行入口，策略、审批、指纹复核、结果归一化 |
| tools | registry.py、builtins.py | 工具注册与 Schema |
| tools | read.py、write.py、edit.py、grep.py、shell.py | 读取、创建/替换、多段编辑、字面量搜索/枚举、受控子进程 |
| tools | files.py、preview.py | 耐久写入、文件版本指纹与审批预览 |
| commands | registry.py、help.py、status.py、model.py、exit.py、sessions.py、context.py | 用户交互命令，直接派发，不进入模型上下文 |
| config | models.py、env.py、connections.py、loader.py | 类型化配置、.env、共享/独立连接解析、显式 TOML 加载 |
| config | workspace.py | 启动目录/projects 默认工作区、显式覆盖、符号链接拒绝 |
| routing | classifier.py、intent.py、router.py、cost.py | 轻量分类、复杂度分流、升级与请求上限、分模型用量 |
| routing | workflow.py、workflow_protocol.py | 规划/编码/审查状态机、结构化产物、阶段私有对话与有界返工 |
| security | policy.py、approval.py、workspace.py | 权限规则、人工授权和路径边界 |
| providers | base.py、scripted.py | 模型协议与确定性离线实现 |
| providers | openai_compatible.py、streaming.py、events.py | HTTP 重试、SSE 分片聚合和传输事件 |
| persistence | session_store.py、models.py | 会话门面和元数据 |
| persistence | journal.py、checkpoint.py、locking.py、storage.py | 原始日志、活动窗口、并发控制、耐久写入 |
| persistence | trace.py、usage.py | 审计轨迹与按 run_id 幂等的用量账本 |
| evaluation | runner.py、cases.json、routing.py | 独立工作区离线验收、复杂度分流与调用分布对照 |
| evaluation | compare_modes.py、mode_tasks.py、mode_task_catalog.py、mode_cases.py、mode_grading.py | 十任务、三种 Agent 架构、重复真实模型评测与分模型核算 |
| interfaces | cli.py、repl.py | 参数解析、终端显示和会话交互 |
| interfaces | terminal_input.py、rendering.py | 注册表驱动斜杠菜单、TTY 降级、流式和最终显示去重 |

tools 的工具类由 ToolExecutor 组合，共用 Workspace 和预算。persistence 的内部 Mixin
由 SessionStore 组合，共用路径、锁和预算；它们不是独立公共存储服务。调用方通过门面使用，
避免分别获取锁而破坏 journal/state/checkpoint 的原有一致性边界。

## 运行协议

1. REPL 追加用户任务，Agent 将完整消息写入 SessionStore。
2. Agent 裁剪旧回合的大工具结果，必要时再压缩活动窗口。
3. Provider 发出 text_delta/tool_call_delta；界面可显示候选文本，工具尚未执行。
4. 正常 finish_reason、完整参数 JSON 和流结束标记全部满足后，生成 ModelReply。
5. Agent 执行 before_tool Hook；继续的操作仍通过 Policy 和 Approval。
6. 标准 ToolResult 写入上下文与原始日志，再反馈给模型。
7. 重复参数和结果达到阈值时停止；模型 final 候选须经过完成验收。
8. RunResult 返回本回合 usage，REPL 幂等保存到会话用量账本。

配置路由时，Agent 在回合开始调用 `RoutedProvider.begin_run(task)`，此时不发请求。
新模板的 `intent_strategy="model"` 在首次请求前由 economy 只分类 `simple/complex`，
宿主选择轻量/主力执行。分类无工具，不展示原文、不进入 Session 消息，但计入 usage。
分类失败转主力；轻量执行仅暴露只读工具，完整响应中的副作用候选仍被拦截并升级。
`model_routed` 的 purpose 区分分类与执行，`intent_classified/route_escalated/routing_summary`
记录决策与请求/Token。默认不需要价格；旧配置未设置策略时保留本地规则。
`config.connections.resolve_connection()` 将共享 API 地址/Key 解析到各 ModelProfile，
Router 按 profile.model 填写请求；共享连接时不更换 Key，独立连接则必须显式配对覆盖。
`/model` 显式切换时新建 Session，旧会话可恢复。

## 并发边界

显式启用 `[workflow]` 后，复杂分支由 `WorkflowCoordinator` 顺序调用规划、编码和审查模型。
阶段数据经固定 JSON 交接，原始推理只用于同阶段工具续接；协调器只发工具候选，实际执行仍
由 Agent/ToolExecutor 完成。`observe_tool()` 回灌真实证据，`finish_run()` 以宿主终态确认完成。
规划/审查只读，审查须重读变更文件，否决有界返工。详见 [多模型工作流](WORKFLOW_PIPELINE.md)。

同一个 Agent 不支持重入；并发任务创建独立实例。同轮工具按顺序执行。
`ReadOnlySubagentTeam` 可让多个独立子 Agent 在单宿主线程池中并行只读探索，等待屏障后把有界、
不可信的公开结论交给唯一可写主 Agent。子 Agent 禁止命令和写入；各自使用独立 Provider、上下文
和 Trace，总 Token 汇总主从及返工。该能力不是跨机器分布式调度。
Eval 使用本机线程并发独立 case。SessionStore 文件锁只覆盖本地存储临界区，
不在等待模型时持有。当前没有分布式调度或 OS 沙箱。

## 稳定性与兼容

- 命令行入口为 `interfaces.cli:main`；`uv run --frozen pi-claw` 保持不变。
- 模块启动使用 `python -m interfaces`；旧 `python -m pi_claw` 不再提供。
- 模块直接导入，例如 `from core.agent import Agent`；移除旧 `pi_claw` 根包转导出。
- 原始会话 journal/checkpoint 格式不变；旧会话没有 usage.json 时按空账本读取。
- 内置 Eval 数据位于 `src/evaluation/cases.json`，wheel/sdist 必须包含它。
- 旧迭代文档记录历史实现；当前路径以本文件和工作流程文档为准。
