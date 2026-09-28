# Pi-Claw 迭代说明书

## 记录规则

每轮优化都新增一份独立说明书，不覆盖旧记录。文件命名为：

```text
ITERATION_<三位序号>_<主题>.md
```

每份说明书至少包含：基线、目标、范围、修改文件、设计取舍、测试证据、技术价值、风险、遗留项
和 Git 状态。状态只使用：

- `开发中`：本轮仍在修改；
- `待用户审核`：代码和测试已完成，但没有用户批准；
- `已批准待提交`：用户明确通过，尚未提交；
- `已提交 optimize`：只提交到优化分支；
- `已合并 main`：用户再次批准后才允许合并；
- `已推送远程`：用户明确批准后才允许 push。

## 迭代索引

| 迭代 | 主题 | 状态 | 核心产物 |
|---|---|---|---|
| [Iteration 001](ITERATION_001_OFFLINE_EVAL.md) | 可量化离线评测基线 | 已提交 optimize | `pi-claw eval`、5 个 case、JSON 报告 |
| [Iteration 002](ITERATION_002_MESSAGE_LOCKS_CONCURRENCY.md) | 消息流、锁、隔离与本地并发 | 已提交 optimize | 7 个 case、并发指标、分布式扩展设计 |
| [Iteration 003](ITERATION_003_VERIFIED_COMPLETION.md) | 工作流梳理与可验证完成 | 已提交 optimize | 宿主完成验收、`unverified`、上游对照 |
| [Iteration 004](ITERATION_004_PACKAGE_LAYOUT_AND_RUNTIME.md) | 包目录与可观测运行时 | 待用户审核 | 分层重构、SSE、usage、重试、Hooks、裁剪和循环检测 |
| [Iteration 005](ITERATION_005_TOOLS_COMMANDS_CONFIG_ROUTING.md) | 工具/命令/配置与成本路由 | 待用户审核 | .env、tools、commands、config、路由升级与成本对照 |
| [Iteration 006](ITERATION_006_DIRECT_SOURCE_LAYOUT.md) | src 下直接组织业务模块 | 待用户审核 | 去掉 pi_claw 中间层、同步导入和构建、目录回归检查 |
| [Iteration 007](ITERATION_007_CHINESE_CODE_COMMENTS.md) | 中文函数说明与接口契约 | 待用户审核 | 补全函数说明、注释关键消息/审批/锁/路由边界、语法树等价检查 |
| [Iteration 008](ITERATION_008_SHARED_MODEL_CONNECTION.md) | 同一 API Key 的多模型路由 | 待用户审核 | 共享连接、仅切换 model ID、独立连接兼容、HTTP 替身验证 |
| [Iteration 009](ITERATION_009_COMPLEXITY_MODEL_ROUTING.md) | 轻量分类与复杂度分流 | 待用户审核 | 无价格配置、分类/执行隔离、主力 fallback、Token 与调用分布验证 |
| [Iteration 010](ITERATION_010_PLAN_CODE_REVIEW.md) | 自动规划/编码/审查 | 待用户审核 | 学校模型 ID、顺序阶段协调、只读审查、有界返工、私有推理续接与离线联调 |
| [Iteration 011](ITERATION_011_WORKSPACE_TERMINAL.md) | 干净工作区与终端交互 | 待用户审核 | projects 默认目录、斜杠菜单、输出去重、425 项测试；固定编码实测通过，auto 尚未通过 |
| [Iteration 012](ITERATION_012_COMMANDS_AND_PROJECT_USAGE.md) | 模型名称、简明状态与项目用量 | 待用户审核 | 唯一退出命令、具体模型 ID 选择、status 精简、跨会话 Token 与缺口提示、444 项测试 |
| [Iteration 013](ITERATION_013_AGENT_EVALUATION.md) | 受限真实 Agent 评测 | 待用户审核 | 评分器校准、独立重复试验、逐项评分与原始轨迹、456 项测试 |
| [Iteration 014](ITERATION_014_LAYERED_EVAL_AND_FAILURE_TRACE.md) | 分层量化与失败追踪 | 待用户审核 | 分层 gate、覆盖率、模型对照、失败 Trace 行号、历史复核、486 项测试 |
| [Iteration 015](ITERATION_015_AGENT_MODE_COMPARISON.md) | 三种 Agent 架构同题对照 | 待用户审核 | 只读子 Agent、唯一写者、12 次同题实测、Token 缺口/耗时/冲突/越权指标 |
| [Iteration 016](ITERATION_016_KIMI_PREMIUM_REVIEW.md) | DeepSeek 默认链路与 Kimi 高级审查 | 待用户审核 | 独立付费连接、确定性风险升级、536 项测试、DeepSeek/Kimi 单请求实测 |
| [Iteration 017](ITERATION_017_CURRENT_MODEL_BASELINE.md) | 十任务当前模型端到端基线 | 已合并 main | 90 个真实 trial、pass@3/pass³、P95、分模型 Token、失败 Trace |
| [Iteration 018](ITERATION_018_PROVIDER_RECOVERY.md) | Provider 失败续跑与完整结果 | 待用户审核 | 身份保持续跑、41/90、0 Provider 错误、累计获取成本 |

Iteration 001–005 保留当时的路径与验证命令，不回写历史记录。当前目录和启动方式以
[Iteration 006](ITERATION_006_DIRECT_SOURCE_LAYOUT.md)、[架构](../ARCHITECTURE.md) 和
[项目说明](../../README.MD) 为准。

2026-09-10：用户明确授权 `git commit`，Iteration 001–003 合并为一次本地 `optimize` 提交。
后续合并 `main` 或推送远程按用户指令执行。
