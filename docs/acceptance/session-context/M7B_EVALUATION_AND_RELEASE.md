# M7b 验收：锚 off/on 对照与双后端综合评测

- 阶段：M7b（含前置：隔离锚开关 + 对照协议冻结）
- 日期：2026-10-14
- 前置：M6b ACCEPTED（后端/命令代码已冻结）
- 主计划：`newplan.md` §8(M7b)
- 结论：**ACCEPTED（精简对照范围，见 §偏离与限制）**

## 前置：锚 off/on 开关与对照协议冻结 —— 结论：ACCEPTED

### 前置-1 隔离的锚开关

- 开关位置：`src/core/task_anchor.py::resolve_anchor_enabled`；REPL 字段 `CodingRepl.task_anchor_enabled`。
- 取值来源：显式构造参数 > 环境变量 `PI_CLAW_TASK_ANCHOR`（`on/1/true/yes` / `off/0/false/no`）> 默认 `on`；
  非法取值显式 `ValueError`，不静默取默认。
- 语义边界（只改一个变量）：`off` **只**跳过本回合的任务锚临时块（`run_turn` 不再 `refresh_anchor`、
  `anchor=None`）；**不**改变 pins 注入、审批、Policy、Sandbox、Skill、默认行为。`off` 时记录
  `anchor_disabled` Trace 事件（含 `run_id`），不静默忽略。
- 验证：`tests/test_anchor_toggle.py` → `4 passed`（优先级、默认注入、off 仅跳过锚而保留 pins、env=off）。

### 前置-2 对照协议（运行前固定，脱敏、无凭据）

| 维度 | 固定值 |
| --- | --- |
| 对照变量 | 仅 `PI_CLAW_TASK_ANCHOR=on|off`；两组其余设置完全一致 |
| 离线机制集 | `context-offline-v1`（`tests/test_context_offline_corpus.py`，9 例，确定性、无模型） |
| 真实模型集 | 两个经典任务：`fizz`（修 bug）、`stats`（加功能 + 约束） |
| 后端 | files 与 sqlite 各跑一遍（M6b 冻结的工厂/迁移） |
| 重复次数 | 每任务每臂 2 次（声明为小样本，不据以夸大结论） |
| Provider / 模型 | OpenAI-compatible，模型 `deepseek-flash`；Skill 设置两组一致（默认 off） |
| 预算 | `--max-steps 12`、`--max-attempts 3`；每例墙钟上限 420s |
| 评分器 | 任务自带 `unittest`（宿主复算），不修改历史评分器或分母 |
| 随机性 | 服务端默认；不可控变量已记录 |
| 成功判据 | 严格成功率（宿主复算测试通过）+ 约束零违反 |
| 指标 | 严格成功率、约束违反、工具调用、总 Token、端到端耗时、后端 parity |

**护栏：** 只有真实模型 off/on 对照完成且观察到约定收益，才允许宣称“降低漂移/提高成功率”；
基线漂移为 0 时如实报告“本次无法证明改善”；失败不得重跑到成功再隐藏；模型或 Docker 不可用时
记录准确阻塞与未覆盖项，不得据此标 M7b 或 M7 ACCEPTED。

## M7b 执行项

- [x] 冻结任务、约束、评分器、模型/Skill 设置、重试和时间预算，再执行对照（§前置-2）。
- [x] 对照 anchor off/on 与 files/sqlite；两组 Skill 设置一致（默认 off），未混合归因。
- [x] 复用 M7a 离线语料（`context-offline-v1`）作为离线门槛。
- [x] 真实模型评测（精简范围）；审核成功/失败/超时/服务错误。
- [x] 完整回归、文档核对与发布审查，写入本文件。

## M7b 执行结果（真实模型，精简对照）

**环境**：provider `openai`（校内平台），模型 `deepseek-flash`；后端 files 与 sqlite 各一组；
每任务每臂 2 次重复；单次墙钟上限 420s；Skill 两组均关闭；sandbox host。
评测入口：`scripts/eval_anchor_compare.py`。

| 后端 | 臂 | n | 严格成功 | 约束零违反 | 工具调用 | 平均 Token | 平均耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| files | on | 4 | 4/4 | 4/4 | 20 | 10563 | 61.1s |
| files | off | 4 | 4/4 | 4/4 | 20 | 10403 | 80.1s |
| sqlite | on | 4 | 4/4 | 4/4 | 20 | 10568 | 67.5s |
| sqlite | off | 4 | 4/4 | 4/4 | 20 | 10273 | 66.4s |

- 全部 16 次运行：测试通过、`test_*` 未被改动、无超时、无“运行未完成”。
- 锚的 Token 开销：files 平均 **+160**（≈+1.5%），sqlite 平均 **+295**（≈+2.9%）；耗时差异为噪声。
- **结论：设置 off/on 后行为无差异**（两臂均 100% 严格成功且零约束违反）。按计划约定，
  本次**不能证明**锚降低漂移或提升成功率，只记录其小额度 Token 开销。files/sqlite 两后端成功一致。

## 偏离与限制（如实声明，不作更强结论）

- 采用**精简对照**：仅 2 个经典任务、每臂 2 次，样本小，**不宣称统计显著性**。
- 任务为单回合短会话，**未覆盖**多次压缩/无关干扰/重启/提示注入的长程漂移场景；因此“防漂移收益”未测。
- 参数经 CLI/REPL 手动运行（`PI_CLAW_TASK_ANCHOR`），未新增自动化 runner 的锚注入接线。
- sandbox 为 host，未做 Docker 复验。

**验收标准 M7b-A：** 离线机制与安全必选项通过；全量 pytest、Ruff、格式、构建、敏感信息检查与
Git diff 审核通过；在线评测按预定义约束与实际结果判断，未出现“以锚在输入中冒充不漂移”的结论。
在**精简范围**内判定为 ACCEPTED；完整协议（更多任务/长会话/重复次数）未执行，不作完整范围声明。

## 证据与结论

- 精简对照：16 次真实模型运行全部通过；报告 `/tmp/m7b-anchor-report.json`、`/tmp/m7b-anchor-report-sqlite.json`。
- 离线机制：`tests/test_context_offline_corpus.py`（9）、`tests/test_anchor_toggle.py`（4）。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 1021 passed, 3 skipped`；Ruff/格式/构建通过；
  敏感信息扫描与 diff 审核通过。

**结论：ACCEPTED（精简范围）**。
