# Pi-Claw 稳定性与发布实施计划

> **执行约定：** 后续阶段均在 `optimize` 分支开发；每个阶段先测试、再实现、全量验证，
> 稳定后提交并推送。用户审核通过后合并 `main`，随后让 `optimize` 快进到新的 `main`。

**目标：** 用可复现的工程门槛和真实模型数据，把 Pi-Claw 从功能完整的本地 Coding Agent
提升为可公开演示、可量化比较、边界表述准确的稳定项目。

**架构原则：** 宿主掌握工具授权和完成验收；模型输出视为不可信候选；离线工程回归与真实模型
能力评测分开；原始 Trace 保持 Git 忽略，只提交任务、评分器和脱敏汇总。

**当前稳定基线：** `639a322`，`optimize` 与 `origin/optimize` 一致。

## 全局约束

- 不提交 `.env`、`pi-claw.toml`、API Key、Base URL、原始 Session 或原始 Trace。
- 非产品材料继续放在仓库外；仓库内只保留代码、测试、架构说明和可复现评测证据。
- 真实评测失败必须进入分母；未知 Token 不按零计算；价格未知时不报告金额。
- Kimi 只用于既有确定性高风险审查策略；评测不为意图分类额外消费 Kimi，并单列其请求、Token
  与 usage 缺口。
- `main` 表示最新稳定版本，未完成工作只存在于 `optimize`。
- 合并前清理已确认冗余的 OS 元数据、缓存和构建产物；不把仍有审计价值的 `runs/` 当垃圾删除。

## 阶段状态总览

| 阶段 | 目标 | 状态 | 完成证据 |
|---|---|---|---|
| 0 | 目录重构、评测框架、DeepSeek/Kimi 分工 | 已完成 | `639a322`；536 tests；已推送 `optimize` |
| 1 | 公开仓库工程化与 CI | 待执行 | GitHub Actions、构建、License、贡献说明 |
| 2 | 当前模型真实评测基线 | **已完成并合并 main** | 90 trial；严格成功 33/90；分模型脱敏报告 |
| 3 | 根据 Trace 修复超时与请求链稳定性 | 待执行 | 修改前后同任务对照；基础设施超时率目标 <5% |
| 4 | 任务相关的完成验收与抗伪完成 | 待执行 | 无关测试、篡改测试、旧绿灯等攻击用例全部拒绝 |
| 5 | 路由校准与 Multi-Agent 故障隔离 | 待执行 | 错误降级率、主力调用率、子 Agent 部分失败指标 |
| 6 | 可选 Sandbox 与正式发布 | 待执行 | 隔离演示、版本标签、两条真实端到端案例 |

---

## 阶段 2：当前模型真实评测基线

### 目的

在不修改 Agent 策略的前提下，把现有两个纯函数任务扩充为十个中等难度任务，并用当前
DeepSeek 配置比较：

1. 单 Agent；
2. 规划—编码—审查工作流；
3. 主 Agent＋两个独立只读子 Agent。

每个任务独立运行三次，共 `10 × 3 × 3 = 90` 个真实 trial。

### Task 2.1：任务和评分契约

**文件：**

- 修改 `src/evaluation/mode_tasks.py`
- 新增 `src/evaluation/mode_task_catalog.py`
- 新增 `src/evaluation/mode_cases.py`
- 修改 `src/evaluation/mode_grading.py`
- 修改 `tests/test_agent_mode_comparison.py`

**接口：**

- `ModeTask` 增加明确的参数名和默认值契约。
- `TASKS` 固定包含十个任务。
- `HIDDEN_CASES[task_id]` 为每个任务提供 pass-to-pass 与 fail-to-pass 隐藏用例。
- `safe_functions()` 从任务定义读取签名，不再维护只含两个函数的硬编码表。

- [x] 先写测试：要求十个任务、签名一致、参考实现全部通过、缺陷实现均被隐藏用例检出。
- [x] 运行专项测试，确认因任务数量和接口缺失而失败。
- [x] 实现任务目录、八个新增任务和隐藏用例。
- [x] 运行专项测试，确认校准通过；并修复校准输入被缺陷候选污染的问题。

十个任务类别：库存原子预留、时间窗口合并、角色权限过滤、余额批量转账、依赖拓扑排序、
事件去重、滑动窗口限流、配置覆盖、Worker 负载分配、乐观版本批量更新。

### Task 2.2：量化指标和公开摘要

**文件：**

- 修改 `src/evaluation/compare_modes.py`
- 修改 `tests/test_agent_mode_comparison.py`

**接口：**

- `summarize()` 增加严格成功数、P50/P95、超时率、状态分布和失败层分布。
- 批次报告增加按模式、按模式＋任务两层汇总。
- 输出目录增加不含绝对路径和逐条 Trace 的 `public-summary.json`。

- [x] 先写失败测试：固定样本必须得到明确 P95、超时率、状态和失败层统计。
- [x] 实现最近秩 P95、按任务汇总和 DeepSeek/Kimi 分模型用量字段。
- [x] 验证公开摘要不包含 workspace、trace、Key、URL。

### Task 2.3：离线完整性和文档

**文件：**

- 修改 `docs/AGENT_MODE_COMPARISON.md`
- 修改 `docs/EVALUATION.md`
- 新增 `docs/iterations/ITERATION_017_CURRENT_MODEL_BASELINE.md`
- 修改 `docs/iterations/README.md`
- 更新本文件阶段状态

- [x] 运行评分器校准，确认不调用模型。
- [x] 运行完整 pytest、Ruff、format、覆盖率和包构建（最终：551 passed、86%）。
- [x] 记录十个任务、90-trial 口径和旧 12-trial 只作历史基线。

### Task 2.4：真实模型批次

- [x] 使用 `pi-claw.toml`、`trials=3`、`workers=2`、三个模式和全部十个任务运行 90 trial。
- [x] 逐项保留失败、Provider 错误、用量缺口和 Trace，不因结果不好删除 trial。
- [x] 核对 Kimi 仅因权限风险或两轮返工触发；11 次尝试、10 次有 usage、已知 125,249 Token。
- [x] 检查公开摘要不含 API Key、Base URL、绝对路径或逐条 Trace。
- [x] 将脱敏汇总写入 Iteration 017；原始产物继续保存在忽略的 `runs/`。

### Task 2.5：清理、提交、推送和稳定分支

- [x] 删除已确认的 `.DS_Store`、测试缓存和构建缓存；保留 `.gitkeep` 与评测证据。
- [x] 确认 Git 跟踪文件中没有冗余二进制、密钥或原始运行产物。
- [x] 再次运行完整验证。
- [x] 提交 `feat(eval): establish ten-task model baseline`。
- [x] 推送 `origin/optimize`。
- [x] 在 `main` 上使用 `--no-commit` 合并，重新验证后提交。
- [x] 推送新的 `origin/main`。
- [x] 让 `optimize` 快进到新 `main` 并推送，作为下一阶段起点。

## 阶段 3：稳定性修复

只根据阶段 2 Trace 中已经发生的失败立项，不预先猜测。重点为连接/读取超时、阶段上下文大小、
取消和重试边界、子 Agent 超时传播、失败请求 Token 缺口。阶段 2 还复现了 DNS 失败和请求级
HTTP 429；应增加跨 trial/Agent 的共享请求节流、Retry-After 退避与可恢复续跑。使用同一批任务
做修改前后对照。

## 阶段 4：语义完成验收

新增宿主侧 `VerificationPlan`，区分必跑与可选检查，建立变更文件和测试的关联，检测测试篡改、
删除、写入后未复测和无关测试绿灯。模型不能自行降低验收要求。

## 阶段 5：路由与 Multi-Agent

建立带标签的 simple/complex 数据集，报告分类准确率和复杂任务错误降级率。增强子 Agent 独立
超时、取消、部分结果交接和总 Token 核算。仍明确限定为单宿主并发，不冒充分布式系统。

## 阶段 6：Sandbox 与发布

在不削弱现有 Policy/Approval 的前提下增加可选隔离执行后端；准备普通缺陷修复和高风险 Kimi
审查两个端到端演示，完成版本标签和公开限制说明。
