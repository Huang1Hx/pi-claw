# Iteration 019：依赖排序语法边界与定向复测

## 状态

已批准待提交。代码、测试和文档仅位于 `optimize` 工作区，尚未提交或推送。本轮不修改模型配置、
隐藏用例、三种 Agent 架构或 Kimi 风险路由，只修复评测契约与审批反馈，并定向复测
`dependency_order`。

## 问题与证据

grader 2.1 的完整 90-trial 中，`dependency_order` 三种模式均为 `0/3`。九份最终候选全部使用
`while`，而旧评分器禁止 `ast.While`，公开任务规格却没有说明这条限制。审批层只返回泛化的
“未获批准”，模型无法知道具体违规节点，也无法据此改写。

这属于评测契约不透明，而不能直接解释为模型不会拓扑排序。评分器会在当前 Python 进程执行候选
函数，因此本轮没有开放无法静态证明终止的 `while`；只开放有明确包围边界的 `for` 循环控制。

## 修改

| 文件 | 修改 |
|---|---|
| `src/evaluation/mode_grading.py` | grader 升至 2.2；允许 `Break`/`Continue`，仍禁止 `While`；超限时返回具体 AST 节点或节点数 |
| `src/evaluation/mode_task_catalog.py` | 在依赖排序规格中公开禁止项，并要求最多按节点数执行的有界 `for` |
| `src/tools/executor.py` | 审批回调兼容原布尔值，并允许用字符串向 Agent 返回具体拒绝原因 |
| `src/evaluation/compare_modes.py` | 将语法检查异常转成“候选代码未通过受限语法检查：具体节点” |
| `tests/test_agent_mode_comparison.py` | 覆盖 `While` 精确反馈、`for` 中 `break/continue`、公开规格和端到端审批结果 |
| `tests/test_tools.py` | 覆盖字符串拒绝原因及旧布尔回调兼容 |

该放宽仍不是通用沙箱：导入、递归、集合、推导式、顶层执行、不受控函数调用和超过 500 个 AST
节点的候选继续被拒绝。

## 定向评测协议

只运行一个任务，保持三个模式、每模式三次、`max_steps=24`、trial 期限 420 秒和单并发：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --task dependency_order --trials 3 --workers 1 \
  --deadline 420 --max-steps 24 \
  --output runs/dependency-order-bounded-syntax-20260929
```

首轮两个 trial 因学校 DeepSeek 服务超时/503 成为 `provider_error`，随后仅按原身份各补跑一次；
没有重跑已完成的语义失败，也没有继续无限重试：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --resume-from runs/dependency-order-bounded-syntax-20260929 \
  --workers 1 \
  --output runs/dependency-order-bounded-syntax-20260929-resume
```

## 结果

grader 2.2 的最终固定分母仍为 9 个 trial：

| 模式 | 严格成功 | pass@3 | pass³ | P50 / P95 | 最终选定已知 Token | 已知 Token/成功 | 终态 |
|---|---:|---:|---:|---:|---:|---:|---|
| 单 Agent | 2/3 | 100% | 0% | 91.605 / 124.168 s | 45,768 | ≥22,884 | 2 completed，1 provider_error |
| 规划—编码—审查 | 1/3 | 100% | 0% | 160.658 / 170.118 s | 176,664 | ≥176,664 | 2 completed，1 provider_error |
| 主 Agent＋子 Agent | 3/3 | 100% | 100% | 165.045 / 204.690 s | 214,905 | 71,635 | 3 completed |

合计严格成功从旧协议下的 `0/9` 变为 `6/9`。更关键的诊断证据是：9 个最终 trial 的
`bounded_candidate` 全部通过，原先的语法白名单阻塞没有再次出现。七个 `completed` 中六个严格
成功；唯一完成但失败的工作流候选把字符串形式的依赖误当作合法列表，在
`dependencies_must_be_list` 隐藏用例上得到 `['a']` 而不是 `[]`。

两次有限补跑后仍有两个 `provider_error`：单 Agent 为读取超时，工作流为学校模型服务 503/不可用。
工作流的补跑已经通过公开测试和大部分隐藏用例，但终态仍是 Provider 错误且遗漏上述类型边界，
因此没有记为严格成功。Kimi 请求为 0；本任务没有触发现有高风险审查策略。

最终选定 9 条的已知 Token 为 `437,337`。获取该结果实际执行 11 个 trial-run：首轮已知
`415,925` Token，加两次补跑 `24,340`，累计获取成本为 `440,265` 个已知 DeepSeek Token。
服务失败请求存在 usage 缺口，因此带 `≥` 的每成功 Token 仍是下界。

## 验证

- 评分器离线校准：10 个参考实现全部通过，10 个初始缺陷均被隐藏用例检出。
- 完整回归：`560 passed in 5.05s`。
- 源代码覆盖率：`85%`（5,625 statements，828 missed）。
- Ruff 检查通过，116 个文件格式检查通过。
- wheel 与 sdist 构建通过。
- `public-summary.json` 未包含 API Key、Base URL、绝对用户路径、workspace 或 Trace 路径。

## 结论与边界

本轮证明原 `0/9` 的主要共同阻塞是未公开的实现约束；修正契约后，三种架构都至少成功一次，主从
模式本批次为 3/3。但样本只有一个任务三次重复，且两个模式仍受 Provider 错误影响，不能据此
宣布主从模式普遍优于单 Agent。完整 90-trial 的 grader 2.1 基线不被回写；grader 2.2 的结果应
作为协议修订后的定向证据，未来只有在预算允许时才能重建完整可比基线。
