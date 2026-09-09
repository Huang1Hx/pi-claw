# 与常见开源 Coding Agent 的工程对照

## 对照范围

本文件只比较公开的架构和功能边界，不复制上游代码，也不宣称 Pi-Claw 与成熟项目功能等价。
来源检查日期：2026-09-06。

| 项目 | 官方公开重点 | Pi-Claw 当前对应 | 本轮取舍 |
|---|---|---|---|
| Kimi CLI | Runtime、Agent Loop、Context/Checkpoint、Approval、Toolset、UI/Wire 分层；可配置 Agent/Subagent 和循环控制 | 已有轻量 Agent/Session/Context/Approval/Tool/CLI 分层 | 保持单 Agent，先补完成验收；Subagent 后置 |
| pi-mono coding-agent | 多 Provider、会话、工具选择、Skills/扩展、事件拦截和运行时切换 | 有 OpenAI-compatible Provider、会话和固定工具注册表 | 不立即做任意扩展加载；先保住 Policy 边界 |
| Aider | Repo map、ask/code/architect 模式、可配置 lint/test | 只有两层目录和入口文档上下文；有受限测试命令 | 先采用写入后验证思想；符号级 repo map 后置 |
| OpenCode | Plan/Build Agent、按工具和模式配置权限、只读 Explore Subagent | 当前用统一 Agent + Policy/Approval | 暂不引入多 Agent 模式；未来可用只读 profile 扩展 |
| OpenHands | EventStream 与隔离 Runtime；Docker Runtime 执行任意代码并控制资源 | 当前是应用级 Workspace/Policy，不是 OS Sandbox | 不安装 Docker，不把白名单命令称为沙箱 |

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

## 暂不采用及原因

### 任意扩展和 MCP 生态

pi-mono 的扩展系统可以注册工具、监听事件和修改行为，但扩展代码拥有宿主权限。Pi-Claw 当前的
核心价值是工具白名单和审批边界；在缺少插件签名、信任来源和能力声明之前，不加载任意扩展。

### 符号级 Repository Map

Aider 的 repo map 会选取重要类、函数、类型和签名，为大型仓库提供更紧凑上下文。Pi-Claw 当前
只读取顶层入口文件和两层目录树。该能力值得后续实现，但需要先定义解析语言、大小预算、增量刷新
和检索效果指标，不能简单把整个仓库塞进 Prompt。

### Subagent/Agent Team

Kimi CLI 和 OpenCode 都公开了隔离上下文的专用 Subagent。Pi-Claw 当前只有本地并发 Eval，尚无
真实 Subagent Runtime。只有在单 Agent 基线稳定，并且同一 Eval 证明成功率或上下文利用得到提升
时再加入。

### Docker Runtime

OpenHands 使用 Docker Runtime 隔离任意代码。Pi-Claw 的 Policy、Approval 和路径限制只是应用层
控制，不等于容器。Docker 仍由用户自行安装；未来 Sandbox Adapter 也不能替代应用层授权。

## 官方来源

- [Kimi CLI repository architecture](https://github.com/MoonshotAI/kimi-cli/blob/main/AGENTS.md)
- [Kimi CLI agents and subagents](https://github.com/MoonshotAI/kimi-cli/blob/main/docs/en/customization/agents.md)
- [pi-mono coding-agent](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent)
- [pi-mono extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md)
- [Aider repository map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md)
- [Aider modes](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/usage/modes.md)
- [Aider lint/test configuration](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/aider_conf.md)
- [OpenCode agents and permissions](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/agents.mdx)
- [OpenHands Runtime architecture](https://github.com/OpenHands/docs/blob/main/openhands/usage/architecture/runtime.mdx)
