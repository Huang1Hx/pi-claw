# M7b 验收：锚 off/on 对照与双后端综合评测

- 阶段：M7b（含前置：隔离锚开关 + 对照协议冻结）
- 日期：2026-10-14
- 前置：M6b ACCEPTED（后端/命令代码已冻结）
- 主计划：`newplan.md` §8(M7b)
- 结论：**IN_PROGRESS**（前置 ACCEPTED；真实模型与双后端对照待执行）

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
| 真实模型集 | `src/evaluation/cases.json` 基线 8 例（`fix_and_verify`、`recover_after_test_failure`、`deny_unapproved_write`、`deny_path_escape`、`safe_max_steps`、`isolation_worker_a`、`isolation_worker_b`、`reject_unverified_completion`） |
| 后端 | files 与 sqlite 各跑一遍（M6b 冻结的工厂/迁移） |
| 重复次数 | 每臂 2 次（声明为小样本，不据以夸大结论） |
| Provider / 模型 | OpenAI-compatible，模型名在运行前记录到结果文件；Skill 设置两组一致 |
| 预算 | 复用现有默认：`--max-steps 12`、`--max-attempts 3`；每例墙钟上限运行前固定并记录 |
| 评分器 | 现有 `evaluation/grading.py`，**不修改**历史评分器或分母 |
| 随机性 | 尽量 temperature=0 / 固定种子；服务不支持时记录为不可控变量 |
| 成功判据 | 严格成功率（宿主验收通过）+ 首次成功率；漂移指标见下 |
| 指标 | 机制：约束保留率、版本正确率、恢复完整率、后端契约通过率；行为：漂移率、严格/首次/重复成功率、重复工具调用、端到端耗时、总 Token、每成功任务 Token；安全：越权/跨工作区/秘密泄露/静默覆盖=0 |

**护栏：** 只有真实模型 off/on 对照完成且观察到约定收益，才允许宣称“降低漂移/提高成功率”；
基线漂移为 0 时如实报告“本次无法证明改善”；失败不得重跑到成功再隐藏；模型或 Docker 不可用时
记录准确阻塞与未覆盖项，不得据此标 M7b 或 M7 ACCEPTED。

## M7b 执行项（待完成）

- [ ] 先冻结任务、约束、评分器、模型/Skill 设置、重试和时间预算，再执行对照。
- [ ] 对照 anchor off/on 与 files/sqlite；两组 Skill 设置一致，不能混合归因。
- [ ] 复用 M7a 场景并扩展双后端、并发及迁移；覆盖多次压缩、无关干扰、目标更新、约束撤销、验证失效、重启与提示注入。
- [ ] 完成离线门槛后才执行真实模型评测；审核所有成功、失败、超时和服务错误。
- [ ] 完整回归、文档核对与发布审查，完成本文件（写入真实结果）。

**验收标准 M7b-A：** 见 `newplan.md` §8(M7b)。当前尚未满足（真实模型与双后端对照待执行）。
