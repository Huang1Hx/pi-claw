# Pi-Claw 稳定性与发布实施计划

> **执行约定：** 后续阶段均在 `optimize` 分支开发；每个阶段先测试、再实现、全量验证，
> 稳定后提交并推送。用户审核通过后合并 `main`，随后让 `optimize` 快进到新的 `main`。

**目标：** 用可复现的工程门槛和真实模型数据，把 Pi-Claw 从功能完整的本地 Coding Agent
提升为可公开演示、可量化比较、边界表述准确的稳定项目。

**架构原则：** 宿主掌握工具授权和完成验收；模型输出视为不可信候选；离线工程回归与真实模型
能力评测分开；原始 Trace 保持 Git 忽略，只提交任务、评分器和脱敏汇总。

**当前稳定基线：** Trace 驱动修复仍冻结为 78/90；Task 3.8 的离线安全门和真实模型诊断续跑
已经完成。把 12 个诊断身份的最新结果合并到相同身份后得到独立派生视图 88/90，但它不是一次
新的完整 90-trial 同协议回归，也不替换 78/90 冻结基线。Task 3.7 的完整回归为 Host 719 项、
Docker 兼容集 713 项（另有 6 项宿主专属契约）。

## 全局约束

- 所有变更按风险划分为可独立审核的批次；每批完成后审核、完整验证、提交并推送开发分支，稳定
  后合并并复验 `main`，具体流程见 `CONTRIBUTING.md`。
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
| 1 | 公开仓库工程化与 CI | **已完成（LICENSE + 通用 CI 首次运行通过）** | MIT `LICENSE`、`.github/workflows/ci.yml`、wheel 元数据 `License-Expression: MIT`；完整 794 项 pytest 通过；GitHub `ci`/`sandbox-security` 均 success |
| 2 | 当前模型真实评测基线 | **已完成并合并 main** | grader 2.2 合并后首次 49/90；两轮修复后 78/90 |
| 3 | Trace 恢复、请求稳定性与执行沙箱 | **当前工程里程碑已收尾；Task 3.3–3.7、Task 3.8 离线安全门和诊断续跑已完成，完整 90-trial 与 holdout 列入后续评测** | 11/11 攻击阻断、30/30 连续运行、12 项诊断最新 10/12、派生视图 88/90 |
| 4 | 任务相关的完成验收与抗伪完成 | **已完成（Task 4.1–4.3）；真实 90-trial/holdout 待冻结后运行** | 篡改/必跑缺失/未复测/无关绿灯被拒；默认入口必跑测试，评测保护 `test_target.py` 并强制关联测试；785 项脱敏 pytest 通过 |
| 5 | 路由校准与 Multi-Agent 故障隔离 | **已完成；Task 5.1 去重、Task 5.2 超时隔离与部分交接、Task 5.3 离线路由校准、Task 5.4 写入副作用修复均已完成** | 去重/发现合并/独立超时/部分交接指标；带标签 28 样本离线校准，错误降级 0、误伤 2；773 项脱敏 pytest 通过 |
| 6 | 正式发布 | 待执行 | 沙箱隔离演示、版本标签、两条真实端到端案例 |
| 7 | 执行生命周期增强：显式事务回滚 | 待执行 | 取消后按显式请求撤销宿主写入，外部改动 fail closed |

---

## 阶段 1：公开仓库工程化与 CI

补齐公开仓库所需的许可证与通用持续集成，让“完整 pytest、Ruff、format、构建”成为每次推送和
PR 的强制门槛，而不是只靠本地手动运行。现有 Docker 沙箱工作流保持不变。

### Task 1.1：许可证与包元数据

**文件：**

- 新增 `LICENSE`（MIT）
- 修改 `pyproject.toml`（PEP 639 `license` 与 `license-files`）
- 修改 `README.MD`（许可证小节）

- [x] 新增 MIT `LICENSE`，版权行为项目作者 `Huang1Hx`。
- [x] `pyproject.toml` 声明 `license = "MIT"` 与 `license-files = ["LICENSE"]`，并确认
  `uv build --no-config` 的 wheel/sdist 元数据包含该许可证文件。

### Task 1.2：通用 CI 工作流

**文件：**

- 新增 `.github/workflows/ci.yml`
- 修改 `tests/test_package_layout.py`（回归：许可证与 CI 存在且覆盖必需命令）

- [x] 新增 `.github/workflows/ci.yml`：`push` 到 `main`/`optimize` 和所有 `pull_request`；Python
  3.12；固定版本 uv；`uv sync --frozen --all-groups`；依次运行 `ruff check`、`ruff format --check`、
  `compileall`、完整 `pytest`、`uv build --no-config`。
- [x] action 版本与 `sandbox.yml` 一致并固定到提交 SHA；`permissions: contents: read`。
- [x] 保留 `.github/workflows/sandbox.yml` 不变；两个工作流职责不重叠。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall、构建通过后再提交。

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

### Task 2.6：Provider 失败恢复

- [x] 增加只续跑 `provider_error` 的 `--resume-from`，保留原 trial 身份和旧 Trace。
- [x] 使用单并发按 `22 → 5 → 1 → 1` 继续原 90-trial 进度。
- [x] 用 grader 2.1 离线重评保留项，最终消除 Provider 错误并得到严格成功 `41/90`。
- [x] 分开报告最终 90 条 Token 与包含 29 次恢复尝试的累计获取成本。
- [x] 验证公开摘要不含 Key、URL、绝对路径、workspace 或逐条 Trace。
- [x] Iteration 018 已提交并推送 `optimize`（`b961e3b`），随后已随阶段 2 合并 `main`。

### Task 2.7：依赖排序评测契约修订

- [x] 定位 `dependency_order` 的九份候选与旧 AST 白名单冲突。
- [x] 公开禁止 `while`、推荐有界 `for`，并允许其中的 `break`/`continue`。
- [x] 审批拒绝返回具体违规语法节点，保留旧布尔审批回调兼容。
- [x] 只重跑依赖排序：首次严格成功 6/9；服务恢复后定向补跑达到 8/9，语法边界 9/9 通过。
- [x] 完整回归 560 passed，源码覆盖率 85%，Ruff、格式和构建通过。
- [x] 用户已批准提交、推送并合并 `main`。

## 阶段 3：稳定性与执行沙箱

阶段 3 同时收口模型请求稳定性和不可信代码执行边界。当前项目已经有应用层安全控制，但不能
称为操作系统沙箱：`Workspace` 限制路径，`WorkspacePolicy` 决定 ALLOW/ASK/DENY，
`TerminalApprovalBroker` 绑定具体预览或 argv/cwd，`ToolExecutor` 复核版本并统一执行，命令使用
`shell=False`、环境白名单和超时。只读子 Agent 不能写入或执行命令，评测 trial 另有独立工作区
和进程组期限。

这些机制能防止常见误操作，却不能阻止获批 pytest/unittest 中的仓库代码读取宿主其他文件、
联网、派生后台进程或消耗宿主资源；命令输出也是捕获完成后才截断。阶段 3 的目标是在不削弱
Policy/Approval 的前提下增加可选 Docker 执行后端，并明确保留 `host` 为“可信工作区兼容模式”。
选择 `docker` 后必须 fail closed：Docker、镜像或隔离参数不可用时直接拒绝，绝不静默回退宿主。

### 当前应用层边界（保留并继续作为第一道门）

| 层 | 当前机制 | 能防什么 | 不能证明什么 |
|---|---|---|---|
| Workspace | 真实路径归一化、拒绝 `..`/越界 symlink、敏感文件名 | 普通文件工具越过项目边界 | 同机恶意进程与检查后换链竞态 |
| Policy | 只读默认允许；写和命令 ASK；危险命令硬 DENY | 模型直接调用未注册、破坏性或联网命令 | 白名单测试代码本身安全 |
| Approval | 一次/会话批准；写入绑定预览与 preimage；命令绑定 argv/cwd | 宽泛授权和审批后普通文件变化 | 用户批准等于 OS 隔离 |
| Executor/Files | 统一入口、精确替换、原子落盘、大小/扫描预算 | 半写、重复匹配、常见并发覆盖 | 多文件事务与恶意跨进程 CAS |
| Command | `shell=False`、环境白名单、30 秒上限、显示输出截断 | Shell 注入、凭据环境继承、普通超时 | 宿主文件/网络/CPU/内存/子进程隔离 |
| Multi-Agent/Eval | 子 Agent 只读；先并行读后主 Agent 写；trial 工作区与期限 | 多 Agent 同时写、评测无限运行 | 分布式容错或生产级沙箱 |

### 阶段 3 执行决策：冻结 78/90，不在同题上追分

Iteration 021 已经提供足够的优先级证据：41 个首次失败经过 55 次修复尝试后恢复 29 个，第一轮
恢复 28 个，第二轮只恢复 1 个；修复增量为 7,800,571 个已知 Token。最终 12 个失败不是 12 个
单纯语义错误，而是 8 个 `deadline`、2 个 `loop_detected`、1 个 `unverified` 和 1 个被请求预算
耗尽误归类的 `provider_error`。Trace 显示多数失败由非固定或非白名单命令被拒后仍反复调用引起。

因此把 `78/90` 和对应 grader、任务、隐藏用例、最大两轮修复规则冻结为阶段 2 基线；不得为达到
`90/90` 修改评分器、放宽安全策略、增加第三语义修复轮次或删除失败。`90/90` 只作为观察到的
stretch result，不是阶段 3 完成门槛。阶段 3 按下列依赖顺序执行：

| 顺序 | 任务 | 先做原因 | 阶段门槛 |
|---:|---|---|---|
| 1 | 3.3 执行后端抽象 | 后续 Host/Docker 共用同一执行与结果契约 | Host 行为兼容，Docker 不可用时 fail closed |
| 2 | 3.4 生命周期与停止语义 | 直接处理剩余失败中的 deadline、loop 和错误终态 | 无新请求越过 deadline；重复拒绝有界停止；进程可清理 |
| 3 | 3.5 临时工作区快照 | Docker 挂载前先消除凭据和宿主目录暴露 | 快照只含普通文件，清单和上限可审计 |
| 4 | 3.6 Docker 隔离 | 在稳定 Runner 和安全快照之上增加 OS 隔离 | 断网、资源限制、非 root、无危险挂载且不回退 Host |
| 5 | 3.7 文件竞态加固 | 补齐 Host 文件工具的 TOCTOU 边界 | symlink/inode 竞态均拒绝，外部文件不变 |
| 6 | 3.8 攻击评测与分层复测 | 先过确定性安全门，再花模型调用验证行为 | 安全硬门槛 100%；12 项诊断复测；90-trial 回归；holdout |

评测代码和真实模型调用只能发生在对应实现与离线回归通过以后，不能边修改边反复运行同一批
12 项。这样既保留可比性，也避免把评测集变成训练集。

### Task 3.1：请求级有限重试

- [x] 找到评测层强制 `max_attempts=1`，确认底层已有安全重试但没有被三模式评测启用。
- [x] DeepSeek 默认最多三次尝试，使用 2 秒、4 秒指数退避并限制单次等待不超过 10 秒。
- [x] 保持鉴权错误和部分流不重试，Kimi profile 继续固定单次尝试。
- [x] 父进程、worker、快照和报告使用同一重试参数；420 秒 trial 上限保持不变。
- [x] 完整回归 562 passed，源码覆盖率 86%，Ruff、格式和构建通过。
- [x] 用户已批准提交、推送并合并 `main`；本轮不新增真实模型复测。

### Task 3.2：Trace 驱动合并与有界候选修复

- [x] 将 `dependency_order` 最终 9 项按稳定身份合入 grader 2.2 的 90-trial 报告。
- [x] 从脱敏 Trace 生成最多两轮修复提示，只继承候选 `solution.py`，不继承被污染 fixture。
- [x] 对瞬时 Provider 失败提供独立续跑；请求预算耗尽不自动重试。
- [x] 首次严格成功 `49/90`，修复后 `78/90`；41 个失败恢复 29 个。
- [x] 保存来源 SHA256、每轮状态、失败尝试、Token、耗时和公开脱敏摘要。
- [x] 完整回归 596 passed，Ruff、格式、构建、报告指纹和公开摘要检查通过。
- [x] Iteration 021 已提交、推送并合并 `main`。

### Task 3.3：执行后端抽象与配置契约

**文件：**

- 新增 `src/sandbox/__init__.py`
- 新增 `src/sandbox/models.py`
- 新增 `src/sandbox/base.py`
- 新增 `src/sandbox/host.py`
- 新增 `src/sandbox/docker.py`
- 修改 `src/config/models.py`、`src/config/loader.py`、`src/tools/shell.py`
- 修改 `src/interfaces/cli.py`、`src/interfaces/repl.py`、`src/commands/status.py`
- 修改 `pyproject.toml`，把 `src/sandbox` 纳入 wheel
- 新增 `tests/test_sandbox_config.py`、`tests/test_sandbox_runner.py`

**接口：**

- `SandboxSettings`：`backend` 只允许 `host|docker`；默认 `host`，Docker 默认 512 MiB、
  1 CPU、128 PIDs、64 MiB tmpfs，禁止网络。
- `CommandRequest(argv, cwd, timeout)`：只接收 Policy 已批准的 argv，不接收 shell 字符串。
- `CommandExecution`：统一返回退出码、有界 stdout/stderr、截断、超时、清理和后端元数据。
- `CommandRunner.run(request, *, control=None) -> CommandExecution`：`HostCommandRunner` 保持兼容；
  `control` 的 deadline/取消行为在 3.4 接入，`DockerCommandRunner` 在 3.6 提供隔离执行。

- [x] 先写失败测试：默认 host、显式 docker、非法 backend/资源值、Docker 不可用时 fail closed。
- [x] 实现配置解析；`.env` 不新增沙箱开关，防止模型连接配置隐式改变执行权限。
- [x] 将 `CommandTools.run_command()` 改为委托 `CommandRunner`，保持现有 ToolResult 合约。
- [x] `--sandbox host|docker` 只覆盖本次启动；`/status` 显示实际后端和资源限制。
- [x] `--yes --sandbox host` 继续打印“不提供 OS 沙箱”的警告；docker 预检失败时在模型调用前停止。
- [x] 配置、CLI、工具和 Host 兼容专项测试通过；完整回归 615 passed，Ruff、format、wheel/sdist
  构建、敏感信息签名扫描和 `git diff --check` 通过。

### Task 3.4：Agent/命令生命周期与停止语义

该任务紧跟 Runner 抽象，先处理 12 个未恢复项暴露的主要根因，再增加 Docker 启动开销。不能靠
提高 420 秒上限或放宽命令白名单换取通过率。

**文件：**

- 新增 `src/core/control.py`
- 修改 `src/core/agent.py`、`src/core/loop_detection.py`
- 修改 `src/routing/router.py`、`src/routing/workflow.py`
- 修改 `src/sandbox/host.py`、`src/tools/shell.py`
- 修改 `src/evaluation/compare_modes.py`、`src/evaluation/report.py`
- 修改 `tests/test_agent.py`、`tests/test_workflow.py`、`tests/test_agent_mode_comparison.py`
- 新增 `tests/test_sandbox_lifecycle.py`

**接口：**

- `ExecutionControl(deadline_monotonic, cancel_event)`：Agent、工作流和命令 Runner 共用剩余时间与取消信号。
- `remaining_seconds()`：任何阶段启动模型请求或命令前先计算剩余预算，不使用各层互不相知的超时。
- `request_budget_exhausted`：请求上限耗尽使用独立终态，不再误报为 Provider 服务错误。
- 重复策略拒绝保留总终态 `loop_detected`，同时记录结构化 `stop_reason=policy_stall`、工具名和策略原因。

- [x] 先写失败测试：相同非白名单命令连续被拒后必须有界停止，不能耗尽整个 420 秒或 36 次请求。
- [x] 外层 trial deadline 传入 Agent、workflow、team leader/subagents 和命令 Runner；剩余时间不足时
  不再启动新模型请求或命令，并把停止原因、剩余预算和当前阶段写入 Trace。
- [x] 保持 420 秒外层上限不变；不得用延长 deadline 掩盖重试、审批反馈或取消传播缺陷。
- [x] 请求预算耗尽、真实 Provider 失败、语义 loop、策略拒绝停滞和用户取消必须使用不同原因；
  旧报告仍可读取，新报告不得把本地预算耗尽计入 Provider 可用性。
- [x] Host 命令使用独立进程组；超时/取消先 TERM，最多等待 2 秒，再 KILL 整组并验证无子进程。
- [x] stdout/stderr 边读边丢弃超出预算的部分并继续 drain 管道，避免“捕获完成后截断”的内存峰值。
- [x] Agent/REPL 中断传递到正在运行的模型请求和命令；恢复会话不自动重放已中断副作用。
- [x] 测试无限循环、重复审批拒绝、输出洪泛、后台子进程、SIGTERM 不退出和并发命令；完成后运行
  Agent、workflow、评测报告和 Host Runner 专项回归。

验证证据：648 项离线测试通过；Ruff、format、wheel/sdist 构建、敏感信息签名扫描和
`git diff --check` 通过。420 秒父级硬上限不变；Task 3.6 接入容器清理后，worker 清理/落盘
预留由 3 秒校准为 8 秒，避免父级先于进程组、容器和快照清理硬终止。

### Task 3.5：无敏感文件的临时工作区快照

**文件：**

- 新增 `src/sandbox/snapshot.py`
- 修改 `src/security/workspace.py`、`src/sandbox/docker.py`
- 新增 `tests/test_sandbox_snapshot.py`

**接口：**

- `WorkspaceSnapshotBuilder.build(source: Workspace) -> WorkspaceSnapshot`
- `WorkspaceSnapshot(path, manifest_sha256, file_count, total_bytes)`

- [x] 先写失败测试：快照遇到 symlink、FIFO 或设备文件必须整体失败，不能跟随或静默跳过；
  `.git`、`.env*`、凭据、`.venv`、`runs` 和 `.pi-claw` 会话目录按固定规则排除，并把相对路径、
  数量和原因写入 manifest；文件数与总字节超过上限必须失败而不是截断后继续。
- [x] 每次命令从已审批工作区创建新的临时快照；只复制普通文件并记录相对路径、大小和 SHA256。
- [x] Docker 只能把快照只读挂载到 `/input`，不能挂载原始工作区、用户目录、Docker socket 或
  其他宿主目录；受信入口把 `/input` 复制到有容量上限的 `/workspace` tmpfs 后再执行命令。
- [x] 容器内命令可写 `/workspace` 以生成缓存/临时文件，但结果不回写快照或原工作区；产品修改
  仍只能走文件工具审批，磁盘洪泛最多耗尽该容器的 tmpfs 上限。
- [x] Docker 后端禁止 `uv sync` 等依赖安装命令；依赖必须预装在固定镜像中，运行时不访问包索引。
- [x] Docker 运行环境固定 `UV_PROJECT_ENVIRONMENT=/opt/venv`、`UV_OFFLINE=1`、
  `UV_NO_SYNC=1`、`UV_NO_ENV_FILE=1`，避免同步、下载或从快照中的 dotenv 注入变量。
- [x] 命令结束或异常后清理快照；清理失败进入 Trace 和最终结果，不能伪装为成功。

验证证据：Host 完整回归 693/693，固定镜像内排除 6 项宿主专属契约后 687/687；初始化失败、
外部硬链接、复制期竞态、快照路径脱敏和清理失败元数据均有回归测试。独立终审 Ready，真实
Docker 退出码 0、`cleanup_failed=false`，且无残留容器或快照。

### Task 3.6：Docker 隔离参数、镜像固定与容器清理

**文件：**

- 新增 `docker/sandbox/Dockerfile`
- 新增 `docker/sandbox/README.md`
- 新增 `scripts/build-sandbox-image.sh`
- 修改 `src/sandbox/docker.py`
- 新增 `tests/test_docker_sandbox_command.py`

**固定执行边界：**

- 受信宿主代码构造 Docker argv，模型永远不能提供 Docker 参数。
- 使用绝对 Docker CLI 路径、`shell=False`、`--pull=never`；预检把配置的 tag 解析为不可变
  image ID，报告中记录 image ID。
- 默认组合 `--network=none`、`--read-only`、`--cap-drop=ALL`、
  `--security-opt=no-new-privileges=true`、`--pids-limit=128`、`--memory=512m`、
  `--memory-swap=512m`、`--cpus=1.0`、非 root UID/GID，以及
  `--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m` 和
  `--tmpfs=/workspace:rw,nosuid,nodev,noexec,size=256m`。
- 唯一宿主挂载是临时快照到 `/input:ro`，容器没有任何宿主读写挂载；不使用 `--privileged`、
  host PID/network、设备映射、Docker socket 或额外 capability。
- 容器环境使用固定白名单；不透传宿主 `PATH`、模型 API Key、代理变量或 Docker 配置路径。

- [x] 先测试完整 argv 与固定顺序，证明用户/模型输入不能插入额外挂载、网络或 capability。
- [x] 镜像只包含锁定 Python/uv 与项目测试依赖，基础镜像按 digest 固定；构建过程生成 SBOM/摘要。
- [x] macOS 使用 Docker Desktop/Colima 的 Linux VM；Linux 推荐 rootless Docker。控制 Docker daemon
  本身属于高权限操作，因此只允许受信 Pi-Claw 宿主进程调用。
- [x] 容器启动失败、镜像漂移、资源参数不受支持时返回明确错误，不降级到 Host。
- [x] Docker CLI 接入 3.4 的 `ExecutionControl`；容器使用不可预测名称，所有退出路径在 `finally`
  中强制删除并确认不存在，清理失败返回 `cleanup_failed`。
- [x] Trace 记录 backend、image ID、网络模式、mount 数、资源上限和清理状态，不记录宿主绝对路径
  或凭据。

### Task 3.7：文件竞态加固

**文件：**

- 新增 `src/security/secure_fs.py`
- 修改 `src/security/workspace.py`、`src/tools/files.py`、`src/tools/read.py`、
  `src/tools/write.py`、`src/tools/edit.py`
- 新增 `tests/test_secure_fs.py`

- [x] 使用 root directory fd 和相对路径逐段访问；可用平台使用 `O_NOFOLLOW`，拒绝任何中间 symlink。
- [x] 在预览、审批后复核和最终打开三个时点核对 inode/type/preimage；原子替换仍在同一目录完成。
- [x] 用测试钩子模拟“校验后把父目录换成 symlink”和“审批后换目标文件”，两者均必须拒绝且
  工作区外文件保持不变。
- [x] 不支持安全 dir-fd 语义的平台在 docker 模式正常工作；host 写模式明确 fail closed 或显示
  降级能力，不能悄悄宣称同等保护。
- [x] 文档继续说明：这些措施减少 TOCTOU 窗口，但不能控制同机特权恶意进程。

### Task 3.8：攻击用例、量化验收和文档

**文件：**

- 新增 `tests/test_docker_sandbox_integration.py`
- 新增 `src/evaluation/sandbox_cases.py`
- 新增 `src/evaluation/mode_holdout_tasks.py`
- 新增 `tests/test_agent_mode_holdout.py`
- 修改 `docs/SECURITY.md`、`docs/RUNTIME.md`、`docs/ARCHITECTURE.md`、`README.MD`
- 新增 `docs/iterations/ITERATION_026_SANDBOX_ATTACK_EVAL.md`
- 新增 `docs/iterations/ITERATION_027_STAGE3_DIAGNOSTIC_CONTINUATION.md`
- 新增 `src/evaluation/mode_diagnostic.py`
- 修改 `src/evaluation/mode_repair.py`、`src/evaluation/compare_modes.py`
- 修改 `docs/iterations/README.md` 和本文件阶段状态

- [x] 使用确定性恶意 fixture 验证：读取宿主文件、读取 `.env`、外联网络、写原工作区、访问
  Docker socket、fork bomb、内存膨胀、磁盘洪泛、无限循环、后台子进程和输出洪泛。
- [x] 必须达到：宿主/凭据泄漏 0、网络成功 0、原工作区命令副作用 0、逃逸成功 0、残留容器/
  进程 0、攻击用例阻断率 100%。
- [x] 超时后 5 秒内完成进程组和容器清理；默认硬限制为 512 MiB、1 CPU、128 PIDs、30 秒。
- [x] 连续运行至少 30 个 sandbox trial，报告完成率、清理率、P50/P95 启动与总耗时、峰值输出
  缓冲、拒绝原因分布；性能只作参考，安全指标是硬门槛。
- [x] Docker 缺失时单元测试照常运行，integration 标记为 skip；发布/CI 的 sandbox job 必须有
  Docker 且不得 skip。若 CI 尚未落地，阶段 3 不能标记全部完成。
- [x] 比较 host/docker 的确定性任务结果；允许耗时不同，不允许评分、文件结果或审批语义漂移。
- [x] **诊断复测与有界续跑：**先运行 Iteration 021 冻结的 12 个未恢复身份，再仅续跑仍失败的
  稳定身份；保持 24 steps、grader 2.2、权限策略与 DeepSeek 模型，不增加第三修复轮次。诊断批次
  采用 1200 秒 trial deadline，因此结果只作为独立诊断和派生视图，不替换 420 秒协议下的
  `78/90` 冻结基线，也不冒充一次新的完整 90-trial 回归。
- [x] 12 个身份最新结果为 10/12；deadline + loop 从原 10/12 降为 0/12，请求预算耗尽误记为
  Provider 错误为 0。剩余 2 项均保留为真实 Provider 错误，根因为 DeepSeek 连接被对端重置；
  失败保留在分母，原始 Trace 继续 Git 忽略。
- [ ] **同协议完整回归：**从干净工作区重新运行 90 trial，并按冻结规则最多修复两轮；最终严格
  成功不得低于 78/90，完整报告必须包含全部首次运行、修复尝试、Token、耗时和失败终态。
- [ ] **新任务 holdout：**在实现冻结后一次性加入至少 3 个同等难度且未参与本阶段调试的任务，
  三种模式各重复 3 次（至少 27 trial），报告严格成功率、pass@3、pass³、P50/P95、总 Token 和
  每成功任务 Token；首次结果作为新基线，不因分数修改任务或删除失败。
- [ ] `90/90` 只能作为 stretch result；阶段完成由确定性安全硬门槛、生命周期目标、原 90 项
  不回归和 holdout 证据共同决定，不能只展示最好的一次模型结果。
- [ ] 在完整 90-trial 与 holdout 完成后，执行阶段 3 最终覆盖率、完整 pytest、Ruff、format、
  wheel/sdist 构建和敏感信息扫描；诊断续跑批次本身的 753 项 pytest、Ruff、format、compileall、
  wheel/sdist 和公开摘要脱敏检查已经通过。
- [x] 明确发布边界：Docker 显著加强命令隔离，但容器仍不是虚拟机，也不能消除内核/daemon、
  镜像供应链或宿主配置风险。

## 阶段 4：语义完成验收

新增宿主侧 `VerificationPlan`，区分必跑与可选检查，建立变更文件和测试的关联，检测测试篡改、
删除、写入后未复测和无关测试绿灯。模型不能自行降低验收要求。

### Task 4.1：宿主侧 `VerificationPlan` 与有计划的完成验收器

**文件：**

- 修改 `src/core/completion.py`（新增 `VerificationPlan` 与 `PlannedCompletionVerifier`，保留
  `ToolEvidenceCompletionVerifier` 兼容）
- 新增 `tests/test_verification_plan.py`
- 修改 `docs/ARCHITECTURE.md`
- 新增 `docs/iterations/ITERATION_032_VERIFICATION_PLAN.md`
- 修改 `docs/iterations/README.md` 和本文件阶段状态

- [x] `VerificationPlan` 为宿主声明、不可变：`required_kinds`（必跑，非空）、`optional_kinds`（可选）、
  `protected_patterns`（受保护文件，fnmatch）、`change_to_tests`（变更文件→关联测试）。
  校验必跑/可选不相交、类别合法、模式非空字符串、映射键唯一，任何非法组合 fail closed。
- [x] `PlannedCompletionVerifier.verify` 检查链：无成功写入→无需验收；写入受保护文件→拒绝
  （测试篡改/删除）；取最后一次成功写入之后的受认可运行；每个 `required_kinds` 必须有成功运行；
  存在 `change_to_tests` 映射时，每个有映射的变更文件必须有成功测试运行引用其关联测试，否则
  拒绝（无关测试绿灯）。
- [x] 保留既有 `ToolEvidenceCompletionVerifier` 行为，不改变现有调用点的默认语义。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再提交。

### Task 4.2：把 `VerificationPlan` 接入 CLI/REPL/工作流（待细化）

- [x] 新增 `DEFAULT_VERIFICATION_PLAN`（必跑 test、可选 lint/compile/lock）与
  `default_completion_verifier()`，接入 `cli.py`（demo/run）、`repl.py` 与工作流编码阶段。
- [x] 默认保持保守：不设 `protected_patterns`/`change_to_tests`，避免误伤合法测试创建；需要
  篡改/无关绿灯检测的宿主显式声明（列入后续）。
- [x] 回归：工作流“写入后只跑 lint”被拒、补跑测试后完成；完整 `pytest`、Ruff、format、
  compileall 通过后再提交。

### Task 4.3：工作区测试保护与变更—测试关联

**文件：**

- 修改 `src/core/completion.py`（`discover_test_files`、`plan_for_workspace`）
- 修改 `src/evaluation/compare_modes.py`（对照评测改用按工作区构造的计划）
- 修改 `tests/test_verification_plan.py`
- 修改 `docs/ARCHITECTURE.md`、`docs/iterations/README.md`
- 新增 `docs/iterations/ITERATION_034_WORKSPACE_TEST_PROTECTION.md`

- [x] `discover_test_files` 只列出运行前已存在的 `test_*.py`/`*_test.py`，不跟随符号链接、跳过
  `__pycache__`；把这些设为受保护，阻止测试篡改/删除，同时不阻止合法的测试创建。
- [x] `plan_for_workspace` 组合既有测试保护与宿主声明的 `change_to_tests` 映射。
- [x] 对照评测 `_agent` 启用：保护 `test_target.py`，要求 `solution.py` 改动伴随真实运行
  `test_target.py`，实现“测试篡改”和“无关测试绿灯”的真实拦截。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再提交。

## 阶段 5：路由与 Multi-Agent

建立带标签的 simple/complex 数据集，报告分类准确率和复杂任务错误降级率。增强子 Agent 独立
超时、取消、部分结果交接和总 Token 核算。仍明确限定为单宿主并发，不冒充分布式系统。

### 阶段 5 执行决策：先做子 Agent 任务去重，不改并发模型

当前 `ReadOnlySubagentTeam`（`src/core/team.py:79`）已经用“只读子 Agent + 屏障 + 唯一写者”
消除了最危险的并发写冲突，但没有解决重复模型调用：`SubagentAssignment` 只含
`name/role/task`（`src/core/team.py:18`），派发前不比较任务语义。误配两个相同任务时，两个
Agent 都会调用模型、消耗 Token、各自交接，由主 Agent 重复核对。

阶段 5 第一步先解决“无意重复”，而不是引入共享记忆、Redis、分布式租约或数据库。原则：

- 无意重复：阻止，并记 `duplicate_assignment_blocked`；
- 有意独立复核：保留，并记 `intentional_redundancy_allowed`；
- 相同发现：按指纹折叠，记 `duplicate_finding_collapsed`；
- 所有实际消耗：继续完整计入评测，不因拦截而丢失记账。

### Task 5.1：子 Agent 任务清单与去重

**文件：**

- 修改 `src/core/team.py`、`src/evaluation/compare_modes.py`
- 修改 `tests/test_team.py`、`tests/test_agent_mode_comparison.py`
- 修改 `docs/ARCHITECTURE.md`、`docs/AGENT_MODE_COMPARISON.md`、`docs/EVALUATION.md`
- 新增 `docs/iterations/ITERATION_028_SUBAGENT_ASSIGNMENT_DEDUP.md`
- 修改 `docs/iterations/README.md` 和本文件阶段状态

- [x] 扩展 `SubagentAssignment`：新增 `objective`、`scope`、`deliverable` 和
  `redundancy_policy="unique"`；保留 `name/role/task` 向后兼容。
- [x] 计算稳定 `work_key = SHA256(objective + scope + deliverable + base_revision)`；
  `objective/scope/deliverable` 任一变化必须改变 key，顺序无关的 scope 需归一化。
- [x] 派发前检测：`work_key` 相同且 `redundancy_policy="unique"` 时只运行一个，另一个记
  `duplicate_assignment_blocked`，不调用模型；`independent_check` 时允许并记
  `intentional_redundancy_allowed`。
- [x] 参与者预期改为按“实际派发集合”推导，替换 `_expected_participants` 的硬编码
  `{"leader","subagent-implementation","subagent-risks"}`；被拦截的子 Agent 不能进入
  expected/unknown 计数，否则 `participants`、`_partial_usage` 和账目会静默错位。
- [x] 交接阶段按 finding 指纹合并相同结论，记 `duplicate_finding_collapsed`；不同结论保留。
- [x] 新增 Trace 事件：`subagent_scheduled`、`subagent_started`、
  `duplicate_assignment_blocked`、`intentional_redundancy_allowed`、`subagent_completed`、
  `duplicate_finding_collapsed`、`subagent_handoff_completed`。
- [x] 新增指标：`duplicate_assignments_detected`、`duplicate_assignments_blocked`、
  `intentional_redundancy_runs`、`duplicate_findings_collapsed`、`handoff_artifact_count`。
- [ ] 估算类指标 `tokens_saved_by_dedup`、`duplicate_tokens_consumed` 暂不实现：被拦截任务没有
  真实调用就没有可信 Token，避免用兄弟任务消耗臆测；待有实测样本后再补。
- [x] 回归：现有 `team` 两个子 Agent（`implementation` 与 `risks`）scope 相同但 deliverable
  不同，`work_key` 必须不同，**不得被误判为重复**。
- [x] 保持只读 + 屏障 + 唯一写者边界；不引入分布式语义、共享 Agent 记忆或跨 Agent 私有上下文。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再
  提交。公开摘要不得新增路径、Trace、Key 或 URL。

### Task 5.2：子 Agent 独立超时、部分交接与 Token 核算

**文件：**

- 修改 `src/core/team.py`、`src/evaluation/compare_modes.py`
- 修改 `tests/test_team.py`、`tests/test_agent_mode_comparison.py`
- 修改 `docs/ARCHITECTURE.md`、`docs/EVALUATION.md`
- 新增 `docs/iterations/ITERATION_029_SUBAGENT_ISOLATION.md`
- 修改 `docs/iterations/README.md` 和本文件阶段状态

- [x] `SubagentAssignment` 增加可选 `timeout_seconds`；`ReadOnlySubagentTeam` 增加
  `subagent_timeout_seconds` 默认值，缺省保持当前“共享外层 deadline”行为。
- [x] 为每个派发子 Agent 派生独立 `ExecutionControl`：`min(外层剩余, now + 超时)`，共享外层
  `cancel_event`，使单个子 Agent 超时/取消不拖垮其余子 Agent。
- [x] 部分交接：一个子 Agent 超时或失败时，主 Agent 仍收到其余已完成结论；超时项在交接产物中
  显式标记 `timed_out`，不得伪装成正常结论。
- [x] 新增指标：`subagent_timeouts`、`failed_subagents`、`partial_handoff`；总 Token 继续包含
  全部实际参与者（含失败/超时项已产生的用量）。
- [x] 外层取消仍必须同时停止全部子 Agent 与主 Agent（保持既有语义与回归）。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再提交。

### Task 5.3：路由校准（simple/complex 数据集与错误降级度量）

**文件：**

- 新增 `src/evaluation/routing_calibration_cases.json`（带标签数据集）
- 新增 `src/evaluation/routing_calibration.py`（加载与离线评估）
- 新增 `tests/test_routing_calibration.py`
- 修改 `src/interfaces/cli.py`（`eval-routing-calibration` 子命令）
- 修改 `docs/EVALUATION.md`、`docs/ARCHITECTURE.md`、`docs/iterations/README.md`
- 新增 `docs/iterations/ITERATION_030_ROUTING_CALIBRATION.md`
- 修改本文件阶段状态

- [x] 数据集：每条含稳定 `id`、`task`、`label∈{simple,complex}`、可选 `note` 与
  `known_downgrade`；覆盖明确只读的简单任务、多步/副作用/含关键词的复杂任务，以及刻意探测
  规则边界的对抗样本（如中文“读取数据并写入新文件”）。
- [x] 加载器 fail closed：`schema_version=1`、非空 `name`、正整数 `version`、非空 `cases`、
  `id` 唯一且非空、`label` 合法；拒绝重复 `id` 与未知字段类型。
- [x] 离线评估 `evaluate_routing_calibration(output)`：对每条运行确定性
  `routing.intent.classify`（rules 策略实际决策）与宿主守卫 `routing.classifier.require_strong`，
  产出两条决策：`rules_decision`（rules 策略真实行为）与
  `guarded_decision = complex if require_strong else rules_decision`（叠加安全网的保守决策）。
- [x] 指标：`rules_accuracy`、`rules_false_downgrades`/`rules_false_downgrade_rate`（complex→simple，
  安全风险）、`rules_false_escalations`（simple→complex，成本）、`guarded_accuracy`、
  `guarded_false_downgrades`；逐条 `rows` 保留 `reason`、`host_guard` 供审计。
- [x] 已知盲点显式化：`known_downgrade` 标记的样本计入 `rules_known_downgrades`，未标记的
  复杂任务错误降级计入 `rules_unexpected_downgrades`，作为回归门（必须为 0）。
- [x] 报告显式声明 offline、固定规则，不调用真实模型、不设价格、不报告节省率。
- [x] 不修改现有分类器行为；校准只度量并暴露盲点（如中文“写入/写到”不在复杂词表），记录为
  后续候选修复，不在本任务内改判定。
- [x] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再提交。

### Task 5.4：修复中文写入副作用盲点并量化误伤

**文件：**

- 修改 `src/routing/intent.py`（新增 `WRITE_SIDE_EFFECTS`）、`src/routing/classifier.py`
- 修改 `tests/test_routing.py`、`tests/test_model_intent_routing.py`
- 修改 `src/evaluation/routing_calibration_cases.json`（升 v2）与 `tests/test_routing_calibration.py`
- 修改 `docs/ARCHITECTURE.md`、`docs/EVALUATION.md`、`docs/iterations/README.md`
- 新增 `docs/iterations/ITERATION_031_ROUTING_WRITE_SIDE_EFFECTS.md`

- [x] 集中定义中文写入/保存副作用动词 `WRITE_SIDE_EFFECTS`，由 rules 分类与宿主 `require_strong`
  守卫共用，消除两处词表漂移（如 `读取数据并写入新文件`、`读取配置并保存` 此前被判简单）。
- [x] 扩充规则测试与宿主守卫测试覆盖中文写入/保存副作用任务。
- [x] 校准数据集升 v2：盲点样本转正、新增只读含“写入”字样的误伤探测样本；指标显示错误降级
  0、未预期降级 0、保守升级（误伤）2。
- [x] 先写失败测试（RED，5 项）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall 通过后再提交。

## 阶段 6：正式发布

阶段 3 沙箱硬门槛通过后，准备普通缺陷修复和高风险 Kimi 审查两个端到端演示；演示必须显示
实际执行后端、镜像 ID、网络与资源限制、审批记录和最终验证证据。随后完成版本标签、变更日志、
安装文档和公开限制说明；未安装 Docker 的用户继续可选可信 host 模式，但不得把它标为 sandbox。

## 阶段 7：执行生命周期增强：显式事务回滚

取消后**不静默回滚**：只有用户显式请求（CLI `--rollback-on-cancel` 或 REPL `/undo`）才撤销本次
回合由宿主写入工具产生的改动。回滚只覆盖宿主追踪的写工具，`run_command` 执行代码产生的外部
副作用（脚本改文件、`__pycache__`、git 操作、网络/进程）无法回滚，必须如实说明。

### Task 7.1：宿主写入日志 `WriteJournal`

**文件：**

- 新增 `src/core/journal.py`（`WriteRecord`、`WriteJournal`）
- 修改 `src/tools/write.py`、`src/tools/executor.py`
- 新增 `tests/test_write_journal.py`

- [ ] `WriteJournal` 在每次**成功提交后**记录 `{path, kind(create|replace), preimage, pre_version,
  post_version}`；策略拒绝、审批拒绝、版本冲突、写入异常都不计入。
- [ ] 创建记录 `preimage=None`（原本不存在），替换记录替换前正文与版本；有界：单条 ≤
  `max_write_bytes`、条目数有上限，超限停止记录并标记日志 `incomplete`（不让内存无界增长）。
- [ ] 单写者语义：子 Agent 只读，只记录主 Agent 提交；顺序即真实提交顺序，供逆序回放。

### Task 7.2：回滚执行与 fail closed

**文件：**

- 修改 `src/security/secure_fs.py`（新增版本校验的删除）、`src/core/journal.py`
- 修改 `src/core/schemas.py`（`RunResult.rollback`）、`src/core/agent.py`（trace/终态）
- 新增 `tests/test_rollback.py`

- [ ] 新增 `SecureFileSystem.atomic_delete_file(relative, expected=version)`：基于 dir-fd、no-follow、
  版本校验的删除，用于撤销 create。
- [ ] `WriteJournal.rollback(secure_fs)` 逆序恢复：replace → 用 preimage 原子写回（`expected=post_version`）；
  create → 版本校验后删除；若当前版本与 `post_version` 不一致（外部已改动）→ **跳过并上报**，绝不覆盖。
- [ ] 返回摘要 `{restored, deleted, skipped, failed}`；`RunResult` 增加 `rollback`，trace 事件
  `rollback_started/rollback_restored/rollback_deleted/rollback_skipped/rollback_completed/rollback_failed`。
- [ ] 回滚本身出错不得伪装成功；部分失败要如实计入 `failed` 并保留可审计原因。

### Task 7.3：显式触发接线

**文件：**

- 修改 `src/interfaces/cli.py`、`src/interfaces/repl.py`
- 修改 `tests/test_terminal_experience.py`、`docs/RUNTIME.md`
- 新增 `docs/iterations/ITERATION_037_EXPLICIT_ROLLBACK.md`

- [ ] `run` 增加 `--rollback-on-cancel`：仅在本次运行以 `CANCELLED` 结束时回滚并打印摘要。
- [ ] REPL 增加 `/undo`：撤销当前会话最近一次已提交写入；无日志或全部跳过时给出明确说明。
- [ ] 无显式标志/命令时保持现状（改动留在工作区），与 `PROJECT_SCOPE.md` 现状一致。
- [ ] 公开摘要与 trace 不得泄漏文件正文或密钥；只输出路径、数量与结果。
- [ ] 先写失败测试（RED）再实现（GREEN）；完整 `pytest`、Ruff、format、compileall、构建通过后再提交。

### Task 7.4：边界与文档

- [ ] 在 `docs/PROJECT_SCOPE.md`、`docs/SECURITY.md`、`docs/RUNTIME.md` 更新：回滚是**显式、仅宿主写入**
  的事务，不是完整文件系统快照，不能撤销命令副作用或外部状态。
- [ ] 说明“取消立即停工具”的既有保证（进程组 20ms 轮询清理）与本次回滚的边界区别。
