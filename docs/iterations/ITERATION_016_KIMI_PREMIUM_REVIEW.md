# Iteration 016：DeepSeek 默认链路与 Kimi 高级审查

## 1. 目标与约束

本迭代把运行时模型分工调整为：学校网关提供的 `deepseek-flash` 承担分类、简单任务、
规划、编码和常规审查；用户自费的 `kimi-k3` 只在重要且必要的代码审查中使用。

必须满足以下约束：

- 删除当前运行配置、模板和现行说明中对 GLM 自动角色的使用，不再向 GLM 发起请求。
- DeepSeek 不参与金额预算选择，但请求数、重试和 Token 仍完整记录。
- Kimi 使用独立 URL 与 API Key，不得继承或泄露学校网关的连接信息。
- Kimi 只能执行只读审查，不能写文件、运行命令或授予权限。
- 普通任务不得调用 Kimi；Kimi 调用失败时不得伪装成高级审查通过。
- 真实密钥只保存在被 Git 忽略且权限受控的 `.env`，不进入 TOML、Trace、Session、文档或测试。

## 2. 配置设计

学校网关继续使用共享连接：

```toml
[connection]
base_url_env = "OPENAI_BASE_URL"
api_key_env = "OPENAI_API_KEY"
```

`economy`、`planner`、`primary`、`reviewer` 四个 profile 都使用 `deepseek-flash`。
保留不同 profile 是为了维持权限、输出上限和阶段统计边界，而不是表示四个不同模型。

Kimi 使用独立 profile：

```toml
[models.premium_reviewer]
base_url_env = "MOONSHOT_BASE_URL"
api_key_env = "MOONSHOT_API_KEY"
model = "kimi-k3"
max_attempts = 1
max_output_tokens = 4096
timeout_seconds = 120
```

配置加载器需要允许模型 profile 同时填写 `base_url_env` 和 `api_key_env`。独立连接必须成对
出现；只换 URL 或只换 Key 都拒绝加载。`max_attempts` 是单 profile 的 HTTP 尝试上限；省略时
沿用命令行全局值，Kimi 显式设为 1，防止付费请求自动重复。

本机 `.env` 从 `/Users/huanghx/API_key/kimi.txt` 同步以下三项，读取和写入过程不打印值：

```dotenv
MOONSHOT_BASE_URL=<来自本机凭据文件>
MOONSHOT_API_KEY=<来自本机凭据文件>
```

模型 ID `kimi-k3` 写在 TOML，不重复写入 `.env`。公开 `.env.example` 只保留变量名和公开地址，
不包含真实 Key。本机 `pi-claw.toml` 与 `.env` 继续由 Git 忽略。

## 3. 工作流与升级规则

默认复杂任务链路为：

```text
DeepSeek 分类 → DeepSeek 规划 → DeepSeek 编码/验证 → DeepSeek 常规只读审查
                                                        ↓ 命中升级规则
                                                   Kimi 高级只读审查
```

Kimi 位于常规审查和宿主最终验收之间。只有常规审查已经批准、写后验证证据存在且需要高级审查
时才进入 Kimi，避免把尚未通过基础检查的内容发送给付费模型。

高级审查使用宿主确定性规则，不再花费一次模型调用判断是否升级。满足任一条件即升级：

1. 用户任务明确要求“Kimi 审查”“高级审查”或 `premium review`。
2. 任务或成功修改的路径涉及认证、密钥、权限、命令执行、文件系统边界、会话持久化、并发、
   锁、模型路由或安全策略。
3. 常规审查经历两轮及以上返工后才批准。
4. 本轮成功修改文件达到 5 个及以上。

配置增加：

```toml
[workflow]
premium_reviewer = "premium_reviewer"
premium_review_policy = "risk"
max_premium_review_requests = 4
```

`premium_review_policy` 仅允许 `off`、`explicit`、`risk`：

- `off`：永不自动使用 Kimi。
- `explicit`：仅明确要求时使用。
- `risk`：使用上述确定性规则；本机模板采用此值。

每个任务最多进入一次 Kimi 高级审查。`max_premium_review_requests=4` 是该次审查内部的逻辑
请求上限，用于批量读取改动文件后提交结构化结论，不代表允许四轮高级返工。

## 4. 权限、结果与失败边界

Kimi 与普通 reviewer 使用相同的结构化审查协议，并必须在当前高级审查阶段重新完整读取本轮
所有修改文件。Kimi 收到的工具 Schema 只包含 `list_files`、`read_file`、`search_text`；返回
写入、命令或未知工具时整组调用被拦截。

高级审查结果：

- `approve`：进入外层宿主最终验收。
- `changes_requested`：停止本轮工作流并标记未通过，不自动再花费 Kimi 请求返工复审。
- 协议错误、超时、鉴权失败或请求上限耗尽：保留错误状态，不回退为“高级审查通过”。

Kimi 的批准仍只是模型审查意见，不能代替真实测试、命令退出码和
`ToolEvidenceCompletionVerifier` 的宿主证据检查。

## 5. 事件、状态和用量

增加 `premium_reviewing`、`premium_review_approved` 工作流阶段，并在路由摘要中记录：

- 是否触发高级审查；
- 触发原因；
- Kimi 逻辑请求数；
- Kimi 审查结论；
- 使用的 profile 和实际模型 ID。

现有 `usage_observed` 和持久用量账本继续记录所有服务端返回的 Token。`/usage` 的总量包含
DeepSeek 与 Kimi；`/status --verbose` 和 Trace 可按 profile/model 查看本回合分布。供应商未返回
usage 时记为未知，不按零计算。任何输出都不得包含完整 API Key。

## 6. 测试与真实验证

先使用替身 Provider 完成离线回归：

1. 普通简单任务和普通复杂任务的 Kimi 请求数均为 0。
2. 显式高级审查、高风险路径、两轮返工和至少 5 个改动文件分别只触发一次高级审查阶段。
3. Kimi 只能看到只读工具，越权候选被拦截。
4. Kimi `changes_requested`、无效 JSON、超时和鉴权错误均不能形成 completed。
5. Kimi profile 使用 Moonshot URL/Key，DeepSeek profile 使用学校 URL/Key，两套密钥不交叉。
6. 配置和异常的 `repr`、Trace、Session、CLI 输出都不包含密钥。
7. 旧配置未声明高级审查时保持兼容，不会意外调用外部服务。

离线全套测试与 lint 通过后才进行真实调用：

1. 使用学校配置最小调用一次 `deepseek-flash`，确认移除 GLM 后默认模型可用。
2. 使用 `premium_reviewer` 固定 profile 对 Kimi 发起一次“只回复 READY”的非流式最小请求，
   `max_attempts=1`；这是本轮唯一主动付费验证。
3. 自动升级链路只用替身测试验证，不额外运行会产生多次 Kimi 费用的完整真实工作流。

真实调用报告只展示模型 ID、成功/失败、耗时和服务端 usage；不展示 Key。若真实 Kimi 调用失败，
保留代码与离线结果，并报告 HTTP/协议诊断，不反复付费重试。

## 7. 修改范围

预计修改：

- `src/config/models.py`、`src/config/loader.py`：profile 连接与请求上限、高级审查配置。
- `src/routing/workflow.py`、`src/routing/workflow_protocol.py`：升级判断和高级只读审查阶段。
- `src/routing/router.py`：profile 重试上限、阶段统计与实际模型记录。
- `src/commands/model.py`、必要的终端渲染：展示高级审查分工和阶段。
- `.env.example`、`pi-claw.toml`、`pi-claw.ustc.example.toml`：DeepSeek 默认角色及 Kimi 独立连接。
- 当前配置、工作流、命令和端到端测试，以及现行 README/配置/工作流说明。

历史迭代文档中对过去 Qwen/GLM 实验及失败原因的客观记录保留；所有“当前配置”“当前自动角色”
描述改为本迭代后的 DeepSeek + Kimi 分工。

## 8. 实施与验证结果

2026-09-27 在 `refactor/package-layout` 分支完成实现，尚未 commit、push 或合并：

- 配置、风险策略、工作流、终端展示和双连接传输均采用测试先行，最终全量 `pytest` 为
  536 passed；`ruff check .`、`git diff --check` 和跟踪文件密钥扫描通过。
- `.env` 中 `MOONSHOT_BASE_URL` 与 `MOONSHOT_API_KEY` 已从本机凭据文件无回显同步，逐项相等；
  `.env` 权限为 0600，`.env` 与 `pi-claw.toml` 继续被 Git 忽略。
- 免费学校连接以固定 `primary` profile 实际请求 `deepseek-flash` 一次，返回 `READY`：
  724 输入 Token、4 输出 Token、总计 728，0 重试。
- 付费连接以固定 `premium_reviewer` profile 实际请求 `kimi-k3` **一次**，返回 `READY`：
  516 输入 Token、32 输出 Token、总计 548，0 重试。没有运行会多次消费 Kimi 的完整在线工作流。
- 两份忽略的冒烟 Trace 分别只出现 `deepseek-flash`、`kimi-k3`；没有 GLM/Qwen 模型 ID、
  `llm_retry` 事件或 API Key。

本轮状态为 `待用户审核`。现有 Iteration 015 评测改动均保留；最终差异由用户审核后再决定提交。
