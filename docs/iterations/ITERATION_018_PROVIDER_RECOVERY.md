# Iteration 018：Provider 失败续跑与完整 90-trial 结果

## 状态

已提交并推送，功能提交为 `b961e3b`；随后随优化分支快进合并并推送到 `main`。本轮不修改
模型分配、任务、隐藏用例、单请求超时或 trial 期限。

## 目标与边界

Iteration 017 的原始 90-trial 批次留下 22 个 `provider_error`，因此只能作为包含外部服务故障的
端到端基线。本轮沿用原 `(mode, task, trial)` 身份，只续跑 `provider_error`：

- 不重跑 `completed`、`loop_detected`、`unverified` 或语义失败来提高分数；
- 每轮写入新目录，不覆盖旧工作区、Trace 或用量证据；
- 恢复结果替换同身份的 Provider 失败项，其余 trial 用 grader 2.1 离线重评；
- 模式、任务、重复次数、模型 ID、120 秒单请求超时、420 秒 trial 期限保持不变；
- Kimi 继续只按既有风险策略触发，不关闭，也不因续跑额外改变路由策略。

## 代码修改

| 文件 | 修改 |
|---|---|
| `src/evaluation/compare_modes.py` | 增加 `--resume-from`、失败项选择、身份校验、原 trial 离线重评和合并报告 |
| `tests/test_agent_mode_comparison.py` | 验证只选择 Provider 失败、保持 trial 身份、替换失败项并升级评分器口径 |
| `docs/EVALUATION.md` | 增加可复现续跑命令和两种成本口径 |
| `docs/AGENT_MODE_COMPARISON.md` | 将当前结果更新为消除 Provider 错误后的 90-trial 报告 |

`--resume-from` 要求新的 `--output` 目录，并拒绝同时指定 `--task` 或 `--mode`。当前模型分配与来源
报告不一致时会在调用模型前停止，避免把不同模型批次悄悄合并。

## 执行过程

原始报告：

```text
runs/agent-modes-current-20260927/report-accounting-v2.json
```

恢复批次依次续跑 `22 → 5 → 1 → 1` 个 Provider 失败项：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --resume-from runs/agent-modes-current-20260927 \
  --workers 1 --output runs/agent-modes-continuation-20260928

uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --resume-from runs/agent-modes-continuation-20260928 \
  --workers 1 --output runs/agent-modes-continuation-20260928-retry2
```

后两轮使用同一命令，来源依次为 `retry2`、`retry3`，输出依次为 `retry3`、`retry4`。最终报告：

```text
runs/agent-modes-continuation-20260928-retry4/report.json
```

最终 `remaining_provider_errors=0`。90 条终态为：

| 终态 | 数量 |
|---|---:|
| `completed` | 68 |
| `loop_detected` | 19 |
| `unverified` | 2 |
| `deadline` | 1 |
| `provider_error` | 0 |

唯一 deadline 是 `workflow/versioned_update-1`，按原协议在 420.012 秒终止；它保留 400,805 个
已知但无法完整归属到模型响应的 Token，并明确标记用量不完整。

## 最终 90-trial 指标

| 模式 | 严格成功 | pass@3 | pass³ | P50 / P95 | 已知 Token | 已知 Token/成功 | usage 缺口 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 单 Agent | 18/30（60.0%） | 80% | 50% | 64.253 / 211.616 s | 982,398 | 54,577.67 | 0/30 |
| 规划—编码—审查 | 10/30（33.3%） | 50% | 20% | 81.128 / 226.823 s | 2,356,069 | ≥235,606.90 | 1/30 |
| 主 Agent＋子 Agent | 13/30（43.3%） | 60% | 20% | 97.010 / 150.882 s | 1,531,647 | 117,819.00 | 0/30 |

合计严格成功 `41/90`，最终选定 90 条的已知 Token 为 `4,870,114`。工作流最终选定结果包含
13 次 Kimi 请求，13 次均返回 usage，已知 Kimi Token 为 `162,337`；单 Agent 和主从模式没有
调用 Kimi。

### 分任务严格成功

| 任务 | 单 Agent | 工作流 | 主从 |
|---|---:|---:|---:|
| 库存预留 | 1/3 | 1/3 | 0/3 |
| 窗口合并 | 3/3 | 3/3 | 3/3 |
| 权限过滤 | 3/3 | 1/3 | 2/3 |
| 余额转账 | 1/3 | 0/3 | 0/3 |
| 依赖排序 | 0/3 | 0/3 | 0/3 |
| 事件去重 | 3/3 | 2/3 | 3/3 |
| 滑动限流 | 3/3 | 3/3 | 2/3 |
| 配置覆盖 | 0/3 | 0/3 | 1/3 |
| Worker 分配 | 3/3 | 0/3 | 2/3 |
| 版本更新 | 1/3 | 0/3 | 0/3 |

消除 Provider 故障后，单 Agent 从 14/30 提升为 18/30，主从模式从 9/30 提升为 13/30，工作流
仍为 10/30。当前数据仍不支持“更多 Agent 天然更可靠”；单 Agent 的成功率、重复成功率和每成功
Token 最好，主从模式只在部分任务增加覆盖，工作流在版本更新任务上出现明显长尾与高返工成本。

## 两种 Token 口径

最终能力表只为每个 trial 选择最新恢复结果，便于保持 90 条固定分母。但获取这份报告时，原始
Provider 失败与恢复尝试已经真实消费 Token，不能从工程成本中删除。累计获取成本为：

| 项目 | 数值 |
|---|---:|
| 实际 trial-run | 119（原始 90＋恢复 29） |
| 已知总 Token | 5,398,767 |
| DeepSeek 已知 Token | 4,799,095 |
| Kimi 已知 Token | 198,867 |
| 无法归属到完整模型响应的已知 Token | 400,805 |
| Kimi 尝试 / 有 usage / 缺口 | 19 / 16 / 3 |

原始与四轮恢复分别消耗已知 Token：`3,444,153`、`1,231,797`、`573,377`、`93,889`、
`55,551`。完整本地核算保存在最终目录的 `recovery-accounting.json`，该目录继续被 Git 忽略。

## 验证与安全

- 新增测试先因续跑接口不存在而失败，实现后通过。
- 完整验证为 `556 passed`；Ruff、116 个文件的格式检查和 wheel/sdist 构建通过；源代码覆盖率
  `85%`（5,616 statements，828 missed）。
- 最终 `public-summary.json` 检查不到 `api_key`、`base_url`、`/Users/`、`workspace` 或
  `trace.jsonl`。
- `.env`、`pi-claw.toml`、原始工作区和 Trace 均未加入 Git。

## 遗留问题

Provider 错误已经通过受控续跑从最终 90 条中消除，但这不是运行时稳定性修复：实际获取过程中仍
出现读取超时，并需要 29 次额外 trial-run。阶段 3 仍需实现共享请求节流、Retry-After、阶段级
检查点和从失败阶段恢复，避免为了补一次高级审查而重新执行完整规划与编码链。
