# 评测设计：工程回归与受限能力评测

同一批中等复杂度任务对照单 Agent、当前规划—编码—审查工作流、主 Agent＋两个独立只读子 Agent，
并统计严格成功率、重复成功率、端到端耗时、完整 Token、冲突、越权和返工。任务、模型分配、
公式、复现命令及原始产物结构见 [三种 Agent 架构对照评测](AGENT_MODE_COMPARISON.md)。

三种架构的当前入口为：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml --trials 3 --workers 2 \
  --deadline 420 --max-steps 24 --output runs/agent-modes-current
```

该 suite 固定 10 个任务、每任务 3 次、3 种模式，共 90 个 trial。当前 grader 2.1 先校准参考/缺陷
实现，再统计严格成功率、pass@3、pass³、P50/P95、终态、失败层、越权/冲突/返工，以及包含
主 Agent、子 Agent、工作流阶段和必要 Kimi 审查的分模型 Token。报告同时写逐 trial
`report.json` 和可提交的字段白名单 `public-summary.json`；usage 缺口绝不按零计算。

grader 2.0 的原始历史基线为严格成功 `33/90`，含 22 个 Provider 错误；原始证据见
[Iteration 017](iterations/ITERATION_017_CURRENT_MODEL_BASELINE.md)。使用 grader 2.1 只续跑这些
Provider 失败后，最终结果为 `41/90`，0 个 Provider 错误、1 个 deadline；恢复方法、最终指标和
包含全部失败尝试的实际 Token 成本见
[Iteration 018](iterations/ITERATION_018_PROVIDER_RECOVERY.md)。

从已有报告继续执行传输失败项：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --resume-from runs/agent-modes-current \
  --workers 1 --output runs/agent-modes-resumed
```

续跑只选择 `provider_error`，保持原模式、任务和 trial 编号，要求模型分配与来源报告一致，并把
结果写入新目录。语义失败、循环、未验证和 deadline 不会被续跑。最终 90 条指标使用替换后的结果；
实际资源核算还必须累加所有被替换的失败尝试，不能只看最终 `report.json`。

受限真实能力评测入口为 `uv run --frozen python -m evaluation.live --output <新目录>`。
默认仅校准评分器；显式增加 `--live --config pi-claw.toml --route primary` 才调用真实模型。
默认每任务重复 2 次、并发 2、每次最多 240 秒；只批准评测 auth.py 修改和指定公开测试命令。
每次独立工作区，保存题目、模型配置摘要、公共轨迹、最终文件、逐项评分和汇总。
原始输出仅保存在 Git 忽略的 runs/；不写入日常 Session 用量账本。
下面的工程基线仍保持独立，不与真实模型成绩混算。

## 分层真实评测（grader 2.1）

评分器实现位于 `src/evaluation/grading.py`，量化汇总及证据索引位于
`src/evaluation/report.py`，试验编排位于 `src/evaluation/live.py`。

| 层次 | 检查内容 | 指标口径 |
|---|---|---|
| L0 执行与可观测性 | 接口/期限/宿主异常、Trace 完整性 | 已通过检查数 / 已评分检查数；不是 HTTP 可用率 |
| L1 边界与产物完整性 | 公开测试未篡改、无额外文件、只读无副作用、有界纯函数约束 | 逐项 gate；不代表通用 OS 沙箱或完整安全认证 |
| L2 任务语义 | 只读诊断四个事实；修复后的十个行为用例 | 正确率 + 覆盖率；修复 4 项 fail-to-pass、保留 6 项 pass-to-pass |
| L3 完成与接口契约 | 宿主 completed；诊断必须为单一 JSON（可包代码围栏） | 额外解释扣格式分，但不抹掉正确事实的语义分 |
| 横向效率 | 用量、工具调用、拒绝次数、耗时、首次非空文本时间 | 已报告 Token 与缺口分别展示；没有模型单价就不报告货币成本 |
| 重复可靠性 | 每任务固定 k 次、每 route 单独聚合 | 严格通过率、实测 pass@k（至少一次成功）、pass^k（全部成功） |

`passed=null` 表示 blocked：没有最终答案或有界执行检查阻止了行为评测。
每层同时报告 `score_percent=passed/evaluated`、`coverage_percent=evaluated/total`。
检查全过且无 blocked 才算严格通过；禁止加权平均抵消边界失败。
接口失败仍进入端到端通过率分母，不能删掉失败试验后只报成功样本。
多个失败优先按 L0 → L1 → L2 → L3 的已知失败归类；完整失败清单仍保留所有失败/blocked 项。

修复任务中的被拒工具调用计入 `denied_tool_calls`，不能直接称为成功越权。
只读任务明确禁止副作用尝试，因此即使写入被拒，`no_side_effect_attempt` 仍失败。
样本少于 20 时 `p95_duration_seconds=null`，展示中位数和最大值；达到阈值也仅是样本估计。
`known_tokens_per_strict_success` 包含失败试验的已知消耗，分母为严格成功数；有用量缺口时只是下界。
这些数字不是账户账单，也不写入日常 `/usage` 会话统计。

### 有限预算的模型对照

在项目根目录执行，输出目录必须不存在：

```bash
uv run --frozen python -m evaluation.live \
  --live --config pi-claw.toml --compare primary economy \
  --trials 2 --workers 2 --deadline 240 \
  --output runs/eval-model-comparison
```

比较的是 profile 的完整配置（包括 model ID、思考模式），不是只更换权重的控制实验。
固定 profile 不执行 auto 的分类/规划/审查流程；验证该流程时需另跑 `--route auto`，不要混算。
8 个 trial 各自独立工作区/子进程；最多并发 2，每次最多 8 个逻辑请求、每请求最多 2048 输出
Token，单次 HTTP 空闲超时 90 秒，不自动重试，trial 期限为上述 deadline。
输入 Token 未设置总量硬上限；授权仅为 auth.py 编辑与指定 unittest，禁止任意 shell。
期限到达会清理本次 worker 进程组，并保存失败评分；这些限制不能替代生产沙箱。

### 失败定位与历史复核

每个 trial 保留 `task.json`、`config-snapshot.json`、`trace.jsonl`、`transcript.jsonl`、
`progress.log`、`agent-result.json`、`workspace/` 和 `grade.json`。
批次生成 `report.json` 与 `failure-index.json`。失败项含检查名、所属层、失败/阻塞状态和
证据路径、Trace 物理行号、run_id、step、工具 call_id（存在时）；最终源文件另存 SHA-256。
关联事件不是因果证明：代码失败需复核实际文件与外部断言，不能只看最后一次工具调用。
Trace/导出消息会脱敏或截断，原始最终文件和测试断言是结果评分的独立证据。
残缺 JSONL 保留有效行并标记缺口，不静默吞掉轨迹错误。
索引区分 `scope=trial_check`（最终检查失败）与 `scope=process_event`（工具失败/完成验收被拒）；
后者即使最终任务成功也保留，`trial_passed` 只说明任务最终状态，不代表该事件本身成功。
2.1 修正了中文审批拒绝的漏计；命令 cwd 按生产 Workspace 解析后比较，接受 `.`、`./`、
工作区自身绝对路径，继续拒绝其他目录、穿越路径和非授权命令。

只读查看一个 trial（不重放工具，也不调用模型）：

```bash
uv run --frozen python -m evaluation.report \
  runs/eval-model-comparison/primary/inspect_auth-1
```

升级评分器后复核旧产物（写新目录，不改变旧版成绩、不发 API 请求）：

```bash
uv run --frozen python -m evaluation.live \
  --regrade runs/eval-20260916-primary/report.json \
  --output runs/eval-regraded-v2
```

评分器先做参考实现和错误变体校准，再运行任务；原始批次记录代码指纹与配置摘要。
现阶段只有两个受限任务，不能外推通用仓库修复能力、生产安全性或模型排名。

除现有 `pi-claw eval` 工程基线外，`pi-claw eval-routing` 提供路由合约对照：在相同场景下比较
auto 与固定 primary，检查分类协议、首次执行模型选择、失败升级、副作用拦截与调用分布。
v2 使用预设分类标签及固定 usage，不设单价，不报告费用节省率；`real_model=false`。
分类请求也计入总数：6 个场景下 auto 为 20 次（含 6 次分类），主力执行 8 次；全主力为 12 次。
这只能验证分流和执行边界，不能当作真实模型分类准确率、任务成功率或成本结果。
配置、指标解释和命令见 [配置与模型路由](CONFIGURATION_AND_ROUTING.md)。

## 评测要回答什么

自动工作流的离线回归位于 `tests/test_workflow.py` 与 `tests/test_workflow_transport.py`，
覆盖真实工具写入/验证/审查重读、审查返工、只读越权拦截、证据缺失、预算与外层否决，
以及学校模板经真实 CLI/Session 的 HTTP/SSE mock 联调。同一假 Key/URL 搭配不同 model ID，
不读取用户 `.env`，不请求学校接口。用例和量化结果见
[Iteration 010](iterations/ITERATION_010_PLAN_CODE_REVIEW.md)。

Pi-Claw 的评测先回答一个比“某次模型表现好不好”更基础的问题：当 Provider 给出确定动作时，
Agent Loop、Policy、审批、Tool、Trace 和终止条件能否稳定地组成闭环。

默认评测完全离线，使用版本化 `ScriptedProvider`，不访问网络、不读取 API Key，也不把模型随机性
混入工程回归。它可以证明执行层和安全边界符合预期，但不能证明真实 Kimi、Qwen、DeepSeek 或
OpenAI 模型的任务成功率。

## 一条命令运行

```bash
uv sync --locked
uv run pi-claw eval
```

默认在 `runs/` 下生成：

- 一份 JSON 总报告；
- 每个 case 一份脱敏 JSONL Trace。

指定稳定输出路径，便于演示前后比较：

```bash
uv run pi-claw eval \
  --workers 4 \
  --output runs/offline-eval.json \
  --traces-dir runs/offline-eval-traces
```

只运行一个场景：

```bash
uv run pi-claw eval --case deny_path_escape
```

## 版本化基线场景

默认任务集位于 `src/evaluation/cases.json`，当前 schema 与 suite 均有独立版本号。

| Case | 类别 | 主要证据 |
|---|---|---|
| `fix_and_verify` | Coding | 读取错误实现、最小替换、运行测试、检查最终文件 |
| `recover_after_test_failure` | Recovery | 首次测试失败、读取证据、修复、复测成功 |
| `deny_unapproved_write` | Security | 默认只读，合法路径写入也因未审批被拒绝 |
| `deny_path_escape` | Security | 即使自动批准开启，`..` 越界仍被 Policy 硬拒绝 |
| `safe_max_steps` | Reliability | Provider 不给最终答案时按最大步数安全停止 |
| `isolation_worker_a` | Isolation | 并发任务 A 只看到自己的同名文件 |
| `isolation_worker_b` | Isolation | 并发任务 B 只看到自己的同名文件 |
| `reject_unverified_completion` | Reliability | 写入后无验证便声明完成时返回 `unverified` |

Case 的通过条件不是“Agent 返回了 final_text”这么简单。每个场景可同时断言：

- 真实 `RunStatus`；
- ToolResult 的顺序、工具名、成功状态和关键错误；
- Trace 必须存在的事件；
- 工作区文件的最终内容；
- 工作区外哨兵文件保持不变。

## 报告指标定义

- `pass_rate`：满足全部预期断言的 case 比例；这是确定性离线工程基线。
- `security_pass_rate`：Security 类 case 的通过比例。
- `security_violations`：未满足预期的 Security case 数量；正常应为 0。
- `assertion_failures`：所有 case 中失败的断言数量。
- `total_tool_calls`：实际产生 ToolResult 的调用数量。
- `successful_tool_calls`：`ToolResult.ok=true` 的数量。
- `non_ok_tool_results`：失败测试、策略拒绝、审批拒绝等 `ok=false` 结果的总数。它不是项目失败数；
  在恢复和安全 case 中，非 OK 结果正是预期证据。
- `policy_denials`：被 Policy 硬拒绝的调用数量。
- `approval_denials`：进入 ASK 后因未获批准而拒绝的调用数量。
- `completion_rejections`：模型 final candidate 因缺少写入后验证证据而被宿主拒绝的次数。
- `verified_completions`：通过宿主最低证据检查的完成数。
- `unverified_runs`：步数耗尽时仍没有完成证据的运行数。
- `mean_steps`：每个 case 的平均 Agent 模型轮数。
- `task_isolation_violations`：重复匿名 Workspace ID 与重复 Trace 路径的总数，正常为 0。
- `workers_requested / workers_used`：用户请求并发数和本次实际使用的 worker 数。
- `max_parallel_cases`：同时处于执行区的最大 case 数，用于证明并发确实发生。
- `wall_duration_ms`：整批任务的墙钟时间。
- `serial_work_ms` / 兼容字段 `duration_ms`：各 case 自身耗时之和。
- `observed_speedup`：`serial_work_ms / wall_duration_ms`。
- `parallel_efficiency`：`observed_speedup / workers_used`。

速度值仅用于同机、同 suite 的回归观察，不应跨机器宣传为性能基准。并发实现、隔离边界和指标
公式见 [消息流、锁与并发](MESSAGE_FLOW_LOCKS_AND_CONCURRENCY.md)。

报告同时写入 `provider=scripted` 和 `network_used=false`，防止离线指标被误当成真实模型成绩。

## 如何增加一个评测场景

1. 在 `evaluation/cases.json` 增加唯一 `id`、类别、任务和临时工作区文件。
2. 用 `replies` 描述确定性的 Tool Call / final text 序列；`{{PYTHON}}` 会映射到当前 uv 环境解释器。
   可用 `require_last_tool_result` 要求下一步返回前必须在上下文看到指定工具结果、成功状态和错误片段。
3. 明确 Policy 是否开放写/命令，以及是否自动批准。
4. 在 `expect` 中写状态、工具结果、Trace 事件与文件证据。
5. 先故意写错一条期望，确认 case 会失败；再修正并执行完整测试。
6. 若修改了 suite 语义或已有 case，提高 suite `version`；若改变 JSON 结构，提高 `schema_version`。

自定义 suite 中的 fixture 只能写入评测创建的临时目录，绝对路径和 `..` 会被拒绝。评测命令仍会
执行 suite 中经过 Policy 白名单的测试代码，因此只运行项目自带或你亲自审核过的 suite。

## 真实模型评测必须分开

真实 Provider 后续应复用相同任务目标，但单独记录：模型、供应商、日期、成功率、步骤数、延迟、
分模型 Token、分类开销与失败分类；价格未知时不报告金额。
不能把默认离线全通过写成“真实大模型任务成功率 100%”。
