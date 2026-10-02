# Iteration 021：Trace 驱动评测合并与候选修复

## 状态

已完成并通过用户验收，代码和文档已提交、推送 `optimize`，并按本轮收尾流程合入 `main`。
核心实现提交为 `bfc79e7`。真实评测使用学校 DeepSeek，Kimi 仅沿用既有风险审查策略。本轮
没有修改隐藏用例、任务答案或 420 秒 trial 上限。

## 基线与目标

输入是两份不可变历史证据：

- grader 2.1 的完整 90-trial 报告，严格成功 `41/90`；
- grader 2.2 的 `dependency_order` 9-trial 定向报告，初次为 `6/9`，另有 2 个
  `provider_error`。

目标是：补齐 2 个传输失败，把新任务结果按稳定身份合并回 90-trial；随后只对失败候选
`solution.py` 做最多两轮 Trace 驱动修复，同时保留首次成绩、每轮 Trace、失败尝试和完整采集成本。

## 实现

| 文件 | 修改 |
|---|---|
| `src/evaluation/mode_reports.py` | 稳定身份、报告兼容性、来源 SHA256、严格子集合并 |
| `src/evaluation/mode_repair.py` | 修复任务选择、Trace 脱敏、干净工作区、瞬时错误分类、修复指标 |
| `src/evaluation/compare_modes.py` | 离线合并、两轮修复、传输续跑、不可变账本与 CLI 编排 |
| `tests/test_mode_reports.py` | 身份、兼容性、指纹、成本血缘与离线 CLI 回归 |
| `tests/test_mode_repair.py` | 证据脱敏、干净工作区、成功即停、成本、传输续跑与 CLI 回归 |
| `tests/test_agent_mode_comparison.py` | seed/prompt 透传和可操作审批反馈回归 |

关键约束：

- 来源报告、工作区和 Trace 不覆盖；每个派生批次写入不存在的新目录。
- 合并按 `(mode, task, trial)` 精确替换，并用当前 grader 2.2 离线重评其余 81 项。
- 修复工作区从任务目录重建，只继承父 `solution.py`，不继承被修改的 fixture。
- 给模型的证据只含失败检查名/层、公开工具错误、审批错误和完成验证错误；隐藏
  expected/actual、Key、URL、绝对路径和完整模型回复均移除，总长最多 8,000 字符。
- `provider_error` 不作为语义证据。只有 Trace 证明是连接重置、超时、429 或 5xx 时才能在新
  批次续跑；请求预算耗尽不自动重试。
- 旧传输失败存入 `superseded_transport_attempts`，即使被新结果替换也继续计入 Token 和耗时。
- 续跑在加载模型配置前核对账本与基线的路径和 SHA256；离线合并拒绝非当前 grader 的替换项。
- Trace 脱敏覆盖通用 POSIX/Windows 绝对路径，而不是只匹配少数本机目录。

## 真实执行结果

### dependency_order 补跑与 90-trial 合并

2 个 DeepSeek `provider_error` 在单并发、每请求最多 3 次尝试下均恢复并严格通过：

| 身份 | 结果 | 耗时 | 已知 Token |
|---|---:|---:|---:|
| single / trial 2 | 15/15，通过 | 83.138 s | 19,601 |
| workflow / trial 2 | 15/15，通过 | 243.423 s | 81,603 |

`dependency_order` 因此由最初定向报告的 `6/9` 提升为 `8/9`。合并报告包含 90 个唯一身份，
grader 为 2.2；81 项从原工作区离线重评，9 项由定向报告替换。合并后的首次严格成功为
`49/90`：single `21/30`、workflow `12/30`、team `16/30`。

实际获取合并基线的成本不是最终 90 行的简单 Token 和：完整 90-trial 来源、原 9-trial 定向
批次及 2 次新补跑合计 `5,408,655` 个已知 Token、`10,300.641` 秒，另有 3 个 usage 缺口。

### 两轮自动修复

41 个首次失败均有候选 `solution.py`，因此进入自动修复。共运行 55 次修复尝试：原始 52 次，
加上 2 个连接重置的传输重跑，以及其中一个身份尚未使用的第 2 语义轮次。

| 指标 | 结果 |
|---|---:|
| 首次严格成功 | 49/90（54.44%） |
| 修复后严格成功 | 78/90（86.67%） |
| 恢复失败 trial | 29/41（70.73%） |
| 第 1 / 第 2 轮恢复 | 28 / 1 |
| 修复增量已知 Token | 7,800,571 |
| 每恢复 trial 的增量已知 Token | 268,985.21 |
| 完整采集已知 Token | 13,209,226 |
| 完整采集耗时 | 21,708.731 s |
| 修复 usage 缺口 | 12 |
| Kimi 请求 / 已知 Token | 6 / 90,363 |
| 越权尝试 / 写冲突 / 返工 | 570 / 0 / 85 |

按模式：

| 模式 | 首次 | 修复后 | 恢复数 | 修复尝试 | 修复已知 Token |
|---|---:|---:|---:|---:|---:|
| single | 21/30 | 25/30 | 4 | 14 | 1,837,237 |
| workflow | 12/30 | 26/30 | 14 | 22 | 3,866,684 |
| team | 16/30 | 27/30 | 11 | 19 | 2,096,650 |

按任务：

| 任务 | 首次 | 修复后 |
|---|---:|---:|
| inventory_reservation | 2/9 | 8/9 |
| window_merge | 9/9 | 9/9 |
| permission_filter | 6/9 | 9/9 |
| balance_transfer | 1/9 | 9/9 |
| dependency_order | 8/9 | 9/9 |
| event_deduplication | 8/9 | 8/9 |
| rate_limit_window | 8/9 | 8/9 |
| config_overlay | 1/9 | 3/9 |
| worker_assignment | 5/9 | 9/9 |
| versioned_update | 1/9 | 6/9 |

该结果衡量的是“在首次失败后获得 Trace 反馈的恢复能力”，不能与首次零反馈成功率混为一谈。
workflow/team 恢复数较多，但增量 Token 和长尾耗时也明显更高。

## 未恢复项与 Trace 根因

最终 12 个失败的状态为 8 个 deadline、2 个 loop_detected、1 个 unverified、1 个
provider_error；按模式为 single 5、workflow 4、team 3。剩余任务集中在 config_overlay 6、
versioned_update 3，另有 inventory_reservation、event_deduplication、rate_limit_window 各 1。

Trace 显示大部分失败反复提交非固定或非白名单命令，获得泛化审批拒绝后继续重试，最终进入
deadline/loop；versioned_update/team/1 写入后始终没有受认可的成功验证，终态为 unverified。
唯一 provider_error 是 workflow/config_overlay/3 已达到本回合 36 次模型请求上限，并非学校
API 瞬时故障，因此没有继续重跑。

根据这组 Trace，本轮进一步让审批层明确返回唯一可写文件、精确验证 argv 和工作区根 cwd，并在
修复提示中直接写出 `uv run python -m unittest -q test_target.py`。该改动有独立回归测试，但没有
用第三语义轮次重跑上述 12 项；`78/90` 仍是严格遵守最多两轮的真实结果。

## 产物与命令

真实产物位于 Git 忽略的目录：

- `runs/dependency-order-bounded-syntax-20260929-retry3/`
- `runs/agent-modes-merged-20260930-grader22/`
- `runs/agent-modes-repair-20260930-rounds2/`
- `runs/agent-modes-repair-20260930-resume-transient1/`

离线合并不调用模型：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --output runs/agent-modes-merged \
  --merge-from runs/agent-modes-continuation-20260928-retry4 \
  --replace-from runs/dependency-order-bounded-syntax-20260929-retry3 \
  --replace-origin runs/dependency-order-bounded-syntax-20260929-resume
```

两轮修复与瞬时错误续跑：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml --workers 1 --repair-rounds 2 \
  --repair-from runs/agent-modes-merged \
  --output runs/agent-modes-repair

uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml --workers 1 \
  --resume-repair-from runs/agent-modes-repair \
  --output runs/agent-modes-repair-resumed
```

## 验证与边界

- 新增测试先分别因模块、CLI、成本血缘、传输续跑和可操作反馈不存在而失败，再由最小实现通过。
- 来源 90-trial、9-trial、补跑、合并和修复报告的 SHA256 均有血缘记录并复核未变化。
- `public-summary.json` 不包含 `/Users/`、Trace 路径、API Key 或模型 URL。
- 聚焦评测回归通过 68 项；完整测试通过 `596 passed in 4.57s`。
- `ruff format --check src tests`、`ruff check src tests`、`compileall` 与 `git diff --check`
  均通过。
- `uv build` 成功生成 `pi_claw-0.1.0.tar.gz` 和 `pi_claw-0.1.0-py3-none-any.whl`。
- 最终账本包含 41 个唯一身份；29 个只运行一轮、12 个运行两轮，2 个被替代传输失败仍保留计费；
  停止原因是 passed 29、round_limit 11、provider_error 1。

自动修复不等于产品自修改：系统只能修改隔离 trial 中的候选 `solution.py`，不会自行修改
Pi-Claw 源码、评分器、任务规格或隐藏用例。当前 `team` 仍是单机线程并发，不宣称分布式容错。
