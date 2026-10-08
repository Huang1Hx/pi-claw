# Iteration 028：子 Agent 任务去重与发现合并

## 状态与边界

状态：实现、离线验证已完成；未调用真实模型。

本轮为阶段 5 Task 5.1 的第一步：在 `ReadOnlySubagentTeam` 前增加任务清单与 `work_key` 去重，
在交接阶段按 finding 指纹合并相同结论。它不引入共享记忆、Redis、分布式租约或数据库，也不改变
“只读子 Agent + 屏障 + 唯一写者”的并发模型。所有实际消耗继续完整计入评测。

## 动机

`SubagentAssignment` 原先只有 `name/role/task`，派发前不比较任务语义。误配两个相同任务时，两个
Agent 都会调用模型、消耗 Token、各自交接，再由主 Agent 重复核对。评测虽然能观测成本，却不能
阻止这种无意重复。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/team.py` | `SubagentAssignment` 增加 `objective`/`scope`/`deliverable`/`redundancy_policy` 与 `work_key`；派发前去重、交接发现指纹合并、`dispatch` 指标 |
| `src/evaluation/compare_modes.py` | `team_assignments()` 抽取并标注范围/交付物；`_expected_participants` 按实际派发集合推导；`build_trial_report`/`summarize` 增加去重指标 |
| `tests/test_team.py` | work_key 稳定性、unique 拦截、independent_check 保留、发现折叠、未知策略 fail closed |
| `tests/test_agent_mode_comparison.py` | 参与者按派发集合推导；内置 team 任务不被误判重复 |
| `docs/ARCHITECTURE.md`、`docs/AGENT_MODE_COMPARISON.md`、`docs/EVALUATION.md` | 边界与指标说明 |

关键约束：

- `work_key = SHA256(objective + 归一化 scope + deliverable)`，scope 顺序无关；缺省字段退化为
  `role`/`task`，保持旧调用点兼容。
- `unique` 策略下相同工作键只运行一个，其余记 `duplicate_assignment_blocked`，**不调用模型**；
  `independent_check` 允许并记 `intentional_redundancy_allowed`。
- 未知 `redundancy_policy` 在构造时 fail closed。
- 被拦截的子 Agent 不产生 Trace，因此参与者预期、`_partial_usage` 的 `unknown_participants` 和
  `participants` 全部改为按实际派发集合推导，替换原先硬编码的
  `{"leader","subagent-implementation","subagent-risks"}`。
- `run()` 只使用局部派发副本，团队实例可重复使用，单次去重不污染注册表。
- 交接阶段按 `" ".join(finding.split())` 的 SHA256 指纹折叠相同结论，不同结论保留。

## 指标

`duplicate_assignments_detected`、`duplicate_assignments_blocked`、`intentional_redundancy_runs`、
`duplicate_findings_collapsed`、`handoff_artifact_count`。总 Token 继续包含全部实际参与者。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   757 passed, 2 skipped
pytest tests/test_team.py tests/test_agent_mode_comparison.py   51 passed
ruff check src tests                            passed
ruff format --check src tests                   138 files already formatted
python -m compileall -q src tests               passed
```

RED 证据：新增 6 个 team 去重用例与 1 个评测参与者用例先失败（`TeamRunResult` 无 `dispatch`、
`SubagentAssignment` 不接受 `redundancy_policy`、`participants` 仍为 3），实现后 GREEN。

## 尚未完成

- 阶段 5 Task 5.2：simple/complex 数据集、分类准确率、错误降级率、子 Agent 独立超时与部分交接；
- 阶段 3 遗留的完整同协议 90-trial 与 holdout（与本次去重无耦合）。
