# Iteration 039：阶段 3 新任务 holdout 基线

## 状态与边界

状态：实现与实跑基线已完成；等待推送 `optimize` 并合并 `main`。

本轮回补齐阶段 3 最后一项遗留：在实现冻结后一次性加入 3 个未参与本阶段调试的中等难度新任务，
并用与核心回归相同的协议跑 27 个 trial（3 任务 × 3 模式 × 3 次）。首次结果即作为新基线，**不因
分数修改任务或删除失败**。holdout 任务只在 `--holdout` 批次使用，不进入默认 90-trial 口径。

边界：holdout 只证明“新任务上的泛化表现”，仍不是通用能力结论；阶段 3 的完成门槛依然是确定性
安全硬门槛 + 生命周期目标 + 原 90 项不回归 + 本 holdout 证据的组合。

## 新增任务（实现冻结前写死，之后不改）

| task_id | 语义 | 初始缺陷（被隐藏用例检出） |
|---|---|---|
| `merge_sorted_streams` | 合并多个已排序流并校验非递减 | 不校验有序性/类型，直接拼接排序 |
| `run_length_encode` | 相邻游程编码，非相邻相同值需分开 | 用字典计数，错误合并非相邻重复 |
| `rolling_max` | 长度不超过 k 的滑动窗口最大值 | 窗口满后清空，重叠窗口结果错误 |

三项均与核心 10 项不重名、不重叠；参考实现满足受限纯函数语法（无导入/递归/推导式），公开测试
覆盖基础路径，隐藏用例覆盖边界、类型与原子性。

## 实现

| 文件 | 修改 |
|---|---|
| `src/evaluation/mode_holdout_tasks.py` | 新增 holdout 定义、`HOLDOUT_TASKS` 与 `ALL_TASKS`，并断言与核心任务不重名 |
| `src/evaluation/mode_cases.py` | 新增三项任务的隐藏用例（含 pass_to_pass/fail_to_pass 分层） |
| `src/evaluation/mode_tasks.py` | 公开 `build_task` 复用同一构建器 |
| `src/evaluation/mode_grading.py` | 任务查找改用 `ALL_TASKS`；`calibrate(task_ids=None)` 默认只校准核心 10 项 |
| `src/evaluation/mode_reports.py`、`src/evaluation/mode_repair.py` | trial 身份与修复工作区解析改用 `ALL_TASKS` |
| `src/evaluation/compare_modes.py` | 新增 `--holdout`，任务选择/校准/评分全部泛化；显式任务与 holdout 互斥 |
| `tests/test_agent_mode_holdout.py` | 覆盖不重名、隐藏用例完备、参考过/初始不过、核心仍为 10 项、CLI 选择与互斥 |

关键设计：

- **不污染默认口径**：`TASKS` 仍是核心 10 项，`--holdout` 才选用 `HOLDOUT_TASKS`；默认校准与
  90-trial 报告字段完全不变。
- **同评分器**：holdout 与核心任务共用 `safe_functions`/`behavior_checks`/`grade`，只多一层任务解析。
- **不按结果调参**：任务、隐藏用例与阈值在本轮之前冻结，实跑前先提交实现。

## 实跑基线（2026-10-10）

协议：grader `agent-modes-2.2`、模型 deepseek-flash、max_steps=24、deadline=420s、workers=3。

| 模式 | 结果 | pass@3 | pass³ | p50 / p95 (s) | 总 Token | 每成功 Token |
|---|---|---|---|---|---|---|
| single | 9/9 | 1.0 | 1.0 | 30.3 / 54.9 | 109,833 | 12,203.67 |
| workflow | 9/9 | 1.0 | 1.0 | 82.1 / 120.8 | 494,262 | 54,918.00 |
| team | 9/9 | 1.0 | 1.0 | 85.1 / 125.1 | 385,882 | 42,875.78 |

- 合计 **27/27**（严格口径 = 可评口径），timeout 0、`provider_error` 0、失败层为空。
- 总 Token 989,977；trial 耗时合计 1,869.4s；未授权尝试共 5、写冲突 0、返工 0。
- 端点本轮已恢复（单 trial 约 25–125s），与上一批降级端点下的 90-trial 成本不可直接比较。

## 稳定验证

```text
pytest                                      820 passed
pytest --cov=src                            TOTAL 86%
ruff check src tests                        All checks passed
ruff format --check src tests               147 files already formatted
python -m compileall -q src                 passed
uv build --no-config                        wheel + sdist passed
凭据签名扫描（Key/Bearer/私有 URL）          0 真实命中（仅占位与脱敏夹具）
git diff --check                            passed
```

`runs/`、`dist/`、`logs/` 由 `.gitignore` 排除；holdout 的完整报告、Trace 与工作区留在本机，
不进入提交。
