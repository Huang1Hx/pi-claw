# 与常见开源 Coding Agent 的工程对照

## 对照范围

本文件只比较公开的架构和功能边界，不复制上游代码，也不宣称 Pi-Claw 与成熟项目功能等价。
来源检查日期：2026-09-10。

| 项目 | 官方公开重点 | Pi-Claw 当前对应 | 本轮取舍 |
|---|---|---|---|
| Pichu | 事件驱动 Agent、流式 Provider、Token/上下文管理、Hooks、重试、循环检测和 Session | 新增类型化事件、SSE 聚合、实际 usage、工具结果裁剪、有限重试、可信 Hook 与循环熔断；原有 Session/审批继续复用 | 不复制实现；保持现有 Policy、journal/checkpoint 与完成验收边界 |
| Kimi CLI | Runtime、Agent Loop、Context/Checkpoint、Approval、Toolset、UI/Wire 分层；可配置 Agent/Subagent 和循环控制 | 已有轻量 Agent/Session/Context/Approval/Tool/CLI 分层 | 保持单 Agent，先补完成验收；Subagent 后置 |
| pi-mono coding-agent | 多 Provider、会话、工具选择、Skills/扩展、事件拦截和运行时切换 | 有 OpenAI-compatible Provider、会话和固定工具注册表 | 不立即做任意扩展加载；先保住 Policy 边界 |
| Aider | Repo map、ask/code/architect 模式、可配置 lint/test | 只有两层目录和入口文档上下文；有受限测试命令 | 先采用写入后验证思想；符号级 repo map 后置 |
| OpenCode | Plan/Build Agent、按工具和模式配置权限、只读 Explore Subagent | 当前用统一 Agent + Policy/Approval | 暂不引入多 Agent 模式；未来可用只读 profile 扩展 |
| OpenHands | EventStream 与隔离 Runtime；Docker Runtime 执行任意代码并控制资源 | 可选 Docker 命令后端；去敏快照、断网/非 root/资源限制和强制清理；Host 仍是可信兼容模式 | 保留 Policy/Approval，在其外增加 fail-closed Docker Adapter，不把容器称为虚拟机 |

## 本轮采用：宿主侧完成验收

Aider 的公开配置支持修改后自动 lint/test；Kimi CLI 的公开架构把循环、工具结果和运行时事件分层。
Pi-Claw 采用与自身架构相容的最小实现：模型仍负责计划，所有验证命令仍经过现有 Policy/Approval，
宿主只依据真实 ToolResult 判定完成，不新增隐式命令或权限。

新增边界：

- 成功写入之后没有成功验证证据时，模型的 final candidate 被拒绝；
- 拒绝原因作为宿主反馈进入下一轮消息；
- 最大步数内仍未验证则返回 `unverified`，不会伪装成 `completed`；
- Trace 记录 `completion_rejected`、`completion_verified` 或 `run_unverified`；
- 离线 Eval 加入虚假完成防护 case 和对应指标。

## 本轮采用：Pichu 运行时能力的边界化实现

Pichu 的公开文档把 Agent 描述为事件驱动运行时，并分别公开了流式 Client、Token 统计、上下文
裁剪/压缩、Hooks、重试与循环检测。Pi-Claw 采用这些工程问题作为能力检查表，但沿用自己的
安全与持久化设计：

- Provider 的 delta 先变成不可执行事件，只有流正常结束、参数 JSON 完整后才产生 ModelReply；
- Token 只记录服务端实际 usage，缺失值不使用字符估算冒充；
- 旧工具正文只在活动上下文中裁剪，append-only journal 保留原始事实；
- Hook 由可信宿主显式注册，只能额外拒绝，不能绕过 Policy/Approval；
- 暂时性请求错误有限重试，但任意流分片已经发出后不重放；
- 重复调用按工具、参数和结果检测，命中后返回独立的 loop_detected 终态。

详细协议和限制见 [运行时说明](RUNTIME.md)。

## 暂不采用及原因

### 任意扩展和 MCP 生态

pi-mono 的扩展系统可以注册工具、监听事件和修改行为，但扩展代码拥有宿主权限。Pi-Claw 当前的
核心价值是工具白名单和审批边界；在缺少插件签名、信任来源和能力声明之前，不加载任意扩展。

### 符号级 Repository Map

Aider 的 repo map 会选取重要类、函数、类型和签名，为大型仓库提供更紧凑上下文。Pi-Claw 当前
只读取顶层入口文件和两层目录树。该能力值得后续实现，但需要先定义解析语言、大小预算、增量刷新
和检索效果指标，不能简单把整个仓库塞进 Prompt。

### Subagent/Agent Team

Kimi CLI 和 OpenCode 都公开了隔离上下文的专用 Subagent。Pi-Claw 已有单宿主
`ReadOnlySubagentTeam`：子 Agent 独立上下文、只读工具和 Token 账本，经等待屏障向唯一可写主
Agent 交接有界结论。它不是跨机器分布式系统；更复杂的子 Agent 工具权限仍要由同题 Eval 证明
收益后再扩展。

### 已采用：受限 Docker 命令 Runtime

OpenHands 使用 Docker Runtime 隔离任意代码。Pi-Claw 现以更小边界实现 `DockerCommandRunner`：
宿主只传固定 CLI argv，工作区先转为去敏只读快照，命令在一次性断网非 root 容器的有界 tmpfs
执行；所有退出路径强制清理。Docker 仍由用户自行安装，Adapter 不替代 Policy/Approval，容器也
不能消除宿主内核、daemon、镜像供应链和配置风险。量化攻击结果见
[Iteration 026](iterations/ITERATION_026_SANDBOX_ATTACK_EVAL.md)。

## 官方来源

- [Pichu repository](https://github.com/yeabwang/pichu)
- [Pichu Agent module](https://github.com/yeabwang/pichu/blob/main/docs/agent-module.md)
- [Pichu Client module](https://github.com/yeabwang/pichu/blob/main/docs/client-module.md)
- [Pichu Context module](https://github.com/yeabwang/pichu/blob/main/docs/context-module.md)
- [Pichu Hooks module](https://github.com/yeabwang/pichu/blob/main/docs/hooks-module.md)
- [Kimi CLI repository architecture](https://github.com/MoonshotAI/kimi-cli/blob/main/AGENTS.md)
- [Kimi CLI agents and subagents](https://github.com/MoonshotAI/kimi-cli/blob/main/docs/en/customization/agents.md)
- [pi-mono coding-agent](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent)
- [pi-mono extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md)
- [Aider repository map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md)
- [Aider modes](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/usage/modes.md)
- [Aider lint/test configuration](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/aider_conf.md)
- [OpenCode agents and permissions](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/agents.mdx)
- [OpenHands Runtime architecture](https://github.com/OpenHands/docs/blob/main/openhands/usage/architecture/runtime.mdx)
