# Iteration 029：子 Agent 独立超时与部分交接

## 状态与边界

状态：实现、离线验证已完成；未调用真实模型。

本轮为阶段 5 Task 5.2 的第一步：为每个派发子 Agent 派生独立截止时间，让单个子 Agent 超时或
失败不拖垮其余子 Agent，并在交接阶段显式标记超时、支持部分交接。它不引入共享记忆、Redis、
分布式租约或数据库，也不改变“只读子 Agent + 屏障 + 唯一写者”的并发模型。外层取消仍同时停止
全部子 Agent 与主 Agent。所有实际消耗继续完整计入评测。

## 动机

Task 5.1 解决了“无意重复调用”，但子 Agent 仍共享外层唯一的 `ExecutionControl`：任一子 Agent
未在总期限内产出，就会连带影响其余仍在工作的子 Agent，且主 Agent 无法区分“正常结论”和“超时
空结论”。本轮让超时相互隔离，并让被截断的项在交接产物中显式可见。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/team.py` | `SubagentAssignment.timeout_seconds`、`SubagentOutcome.timed_out`、`TeamRunResult.handoff_artifacts`、`ReadOnlySubagentTeam.subagent_timeout_seconds` 与 `_child_control`；`dispatch` 新增隔离指标 |
| `src/evaluation/compare_modes.py` | `build_trial_report`/`summarize` 新增 `subagent_timeouts`、`failed_subagents`、`partial_handoff`/`partial_handoff_trials` |
| `tests/test_team.py` | 超时隔离 + 部分交接、非正超时拒绝 |
| `tests/test_agent_mode_comparison.py` | 部分交接指标进入 trial 报告 |
| `docs/ARCHITECTURE.md`、`docs/EVALUATION.md` | 超时隔离、部分交接与指标说明 |

关键约束：

- `SubagentAssignment.timeout_seconds` 与 `ReadOnlySubagentTeam.subagent_timeout_seconds` 均为可选，
  缺省保持“共享外层 deadline”的既有行为；非正数或布尔值在构造时 fail closed。
- `_child_control` 为每个子 Agent 派生 `ExecutionControl`：`deadline = min(now + 超时, 外层剩余)`，
  并**共享外层 `cancel_event`**，因此外层取消对所有子 Agent 仍即时生效。
- 子 Agent 以 `RunStatus.DEADLINE` 结束且其 `ExecutionControl` 确实是被派生的独立对象时，记
  `timed_out=True` 并发 `subagent_timed_out` Trace；该标记写入交接产物 `timed_out` 字段，
  绝不伪装成正常结论。
- 部分交接：一个子 Agent 超时/失败时，主 Agent 仍收到其余已完成结论；`handoff_artifacts` 暴露
  全部交接产物（含超时项）。
- 新增指标：`subagent_timeouts`、`timed_out_assignments`、`failed_subagents`、
  `failed_assignments`、`partial_handoff`（同时存在失败与成功项时才为真）。
- 总 Token 继续包含全部实际参与者，含失败/超时项已产生的用量。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   760 passed, 2 skipped, 4 deselected
pytest tests/test_team.py tests/test_agent_mode_comparison.py   55 passed
ruff check src tests                            All checks passed
ruff format --check src tests                   138 files already formatted
python -m compileall -q src tests               passed
```

RED 证据：新增 team 超时隔离用例与评测部分交接用例先失败（`SubagentAssignment` 不接受
`timeout_seconds`、`TeamRunResult` 无 `handoff_artifacts`、trial 报告无 `partial_handoff`），
实现后 GREEN。

## 尚未完成

- 阶段 5 Task 5.3：带标签的 simple/complex 数据集、分类准确率与复杂任务错误降级率（路由校准）；
- 阶段 3 遗留的完整同协议 90-trial 与 holdout（与本次隔离无耦合）。
