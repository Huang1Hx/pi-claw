# Iteration 038：阶段 3 同协议 90-trial 回归与模型失败口径修正

## 状态与边界

状态：实现与离线验证已完成；等待推送 `optimize` 并合并 `main`。

本轮回应用户两条要求：其一，补齐阶段 3 遗留的同协议完整 90-trial 回归；其二，明确“模型/端点
传输失败不计入评测失败”。回归在降级端点下通过“修复续跑 + 单个 provider_error 复测”达成
严格 90/90；口径修正让 `provider_error` 从评测分母中剔除并单独计数，同时保留原严格口径。

边界：`90/90` 仍只是观察到的 stretch result，不是阶段 3 完成门槛；新任务 holdout 仍未执行，
阶段 3 不因本轮回而整体标记完成。

## 动机

- 阶段 3 冻结基线为 420 秒协议下的 78/90。端点退化后（单请求实测 36–49 秒，约为此前 7–10 倍），
  420 秒会把“慢但正确”误判为 deadline，故诊断路径允许显式放宽 deadline（上限 900→1800 秒）。
- 上一批 live 回归在 deadline=1800 下得到 58/90，其中 10 项为 `provider_error`、1 项 deadline；
  仅续跑 provider_error 后升至 63/90，剩余 26 项为可修复的语义失败。
- 修复账本显示 26 项语义失败在一轮修复内全部恢复（recovery_rate 1.0）。最后仅剩
  `single/config_overlay/1` 一项 provider_error。本轮对该项单独复测，端点正常返回后通过，
  得到严格 90/90。

## 实现

| 文件 | 修改 |
|---|---|
| `src/evaluation/compare_modes.py` | 新增 `MODEL_FAILURE_STATUSES` 与 `gradable_failures()`；`summarize` 增补 `gradable_*` 与 `excluded_model_failure_trials`；live/repair 退出码改用可评失败数 |
| `src/evaluation/mode_repair.py` | `summarize_repairs` 的 `post_repair` 增补 `gradable_successes`/`gradable_failures`/`excluded_model_failure_trials` |
| `tests/test_agent_mode_comparison.py` | 新增 `test_summary_excludes_model_failures_from_evaluation_denominator` |
| `scripts/eval_progress.py` | 只读评测进度看板（阶段 3 长跑观察用） |
| `scripts/materialize_postrepair.py` | 把修复账本叠加到来源报告，物化完整 90 行报告根，供剩余 provider_error 复测接续 |

关键设计：

- **分母修正不改严格口径**：`strict_successes/strict_failures` 保持“全部 trial”原义；
  `gradable_*` 才是剔除模型失败后的评测口径，两者并存避免历史报告语义漂移。
- **只剔除 `provider_error`**：`harness_error`、`deadline` 仍计入可评失败并保持可追踪；模型失败
  单独暴露为 `excluded_model_failure_trials`。
- **退出码跟随可评口径**：只有真实实现失败才返回非零，模型/端点抖动不再让批次“失败”。
- **续跑不改原目录**：复测只读来源工作区做离线重评，原 Trace 与首次结果保持字节级不变。

## 验证

- 阶段 3 同协议 90-trial 最终结果：**严格 90/90**（single 30/30、workflow 30/30、team 30/30），
  grader `agent-modes-2.2`，`remaining_provider_errors=0`。
- 血缘成本：基线采集 5,112,773 tokens / 45,786.8s；修复 1,664,360 tokens / 6,869.9s；
  合计 6,777,133 tokens / 52,656.7s。增量安全：未授权尝试 10、写冲突 0、返工 0。
- `pytest`：813 passed（本机全部通过，无 deselect）。
- Ruff check、Ruff format、compileall 通过。

## 后续

- 新任务 holdout（≥3 任务 × 3 模式 × 3 trial）尚未执行，实现冻结后一次性加入。
- 阶段 3 最终覆盖率、wheel/sdist 构建、敏感信息扫描与 holdout 完成后统一收口。
