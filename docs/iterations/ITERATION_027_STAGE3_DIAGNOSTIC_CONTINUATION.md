# Iteration 027：Stage 3 诊断续跑与派生 90 项视图

## 状态与边界

状态：评测、合并与本地稳定验证已完成。

本迭代先续跑 12 项诊断结果中仍失败的 5 个稳定身份，再按用户要求只续跑其中仍失败的 3 个身份。
成功 trial 在 grader 满足全部检查后立即结束，不继续调用模型；已经成功的身份不重复运行。
`78/90` 仍是 Iteration 021 的冻结基线，最终 `88/90` 是用同身份最新诊断结果替换后的独立派生
视图，不是一次新的完整 90-trial 回归。

原始报告、工作区、Prompt、Session 和 Trace 保存在 Git 忽略的 `runs/`，不提交 API Key、Base URL、
宿主绝对路径或未脱敏运行产物。仓库只记录可复现协议、聚合数字与验证结果。

## 续跑与合并机制

`mode_diagnostic` 和 `compare_modes` 增加 fail-closed 的诊断续跑入口：

1. 加载原 12 项诊断和先前 6 项部分重跑，校验文件摘要、预期条数和稳定身份；
2. 按 `(mode, task, trial)` 合并最新结果，只选择仍失败的 5 项；
3. 在任何模型调用前校验冻结报告、repair ledger、历史成本账本和协议参数；
4. 沿用来源批次的单 worker、1200 秒 trial deadline、24 steps，以及 3 次请求尝试和 2/10 秒
   有界退避；
5. 将 5 项结果合回 12 项诊断，再覆盖到物化后的冻结 90 项身份，生成独立派生报告；
6. 链式续跑直接读取该派生报告，只选择最新视图中的 3 个失败身份，并验证前一报告哈希、冻结
   血缘和累计成本，不需要把多个 grade 目录手工拼接；
7. 获取成本累计原 12 项、先前 6 项、首批 5 项与末批 3 项，共 26 次诊断运行；替换失败行不会
   删除历史消耗。

首次续跑要求显式给出预期失败数和先前替换数；链式续跑只接受派生报告和预期失败数。来源漂移、
身份重复、条数不符、哈希不符、历史成本缺失或协议不一致均在调用模型前失败。

## 本轮 5 项结果

| 身份 | 结果 | 检查 | 耗时 | 已知 Token | 终态 |
|---|---:|---:|---:|---:|---|
| workflow / event_deduplication / 3 | 失败 | 4/15 | 335.739s | 7,198 | provider_error |
| single / config_overlay / 1 | 失败 | 14/15 | 486.872s | 6,124 | provider_error |
| workflow / config_overlay / 1 | 成功 | 15/15 | 1052.787s | 199,864 | completed |
| single / config_overlay / 2 | 成功 | 15/15 | 871.066s | 122,678 | completed |
| workflow / config_overlay / 3 | 失败 | 4/15 | 549.196s | 23,499 | provider_error |

本轮合计 2/5 成功，已知 Token 359,363，总耗时 3295.660 秒，3 项 usage 不完整。5 项均只调用
`deepseek-flash`；没有新增 Kimi 请求。三项失败均在有限重试后收到
`[Errno 54] Connection reset by peer`，因此如实保留为 Provider 错误，不改判为任务失败或超时。

## 末批 3 项结果

| 身份 | 结果 | 检查 | 耗时 | 已知 Token | 终态 |
|---|---:|---:|---:|---:|---|
| workflow / event_deduplication / 3 | 失败 | 4/15 | 645.577s | 34,542 | provider_error |
| single / config_overlay / 1 | 失败 | 13/15 | 310.849s | 1,694 | provider_error |
| workflow / config_overlay / 3 | 成功 | 15/15 | 865.933s | 126,769 | completed |

末批合计 1/3 成功，已知 Token 163,005，总耗时 1822.359 秒，2 项 usage 不完整。3 项均只调用
`deepseek-flash`，没有新增 Kimi 请求；两项失败仍是在有限重试后由对端重置连接。成功项达到
15/15 后立即结束，没有启动额外轮次。

## 合并后的量化结果

| 口径 | 结果 |
|---|---:|
| 12 项诊断最新严格成功 | 10/12（83.33%） |
| 12 项最新 deadline + loop | 0/12（原 10/12） |
| 请求预算耗尽误记 Provider 错误 | 0 |
| 12 项已知 Token / usage 不完整 | 1,213,605 / 2 项 |
| 12 项总耗时 | 9045.466 秒 |
| 派生 90 项：single | 29/30 |
| 派生 90 项：workflow | 29/30 |
| 派生 90 项：team | 30/30 |
| 派生 90 项合计 | 88/90（97.78%） |

派生 90 项仍包含历史基线中 workflow 的 9 次 Kimi 审查请求；两批续跑均没有产生 Kimi 请求。
完整获取账本累计冻结阶段和 26 次诊断运行，已知 Token 为 15,103,555，总耗时为 39,071.868 秒，
另有 31 个 usage 不完整的 trial。这里的成本不能用“最终保留的 90 行”倒推，因为被替换的失败尝试也
必须计入。

## 一致性与隐私验证

- 派生报告恰好 90 个唯一身份，诊断恰好 12 个唯一身份，两批分别恰好 5 和 3 个唯一身份；
- 派生成功/失败计数与 88/2 一致，诊断成功/失败计数与 10/2 一致；
- 原诊断报告、冻结 repair 报告、repair ledger 和基线报告的 SHA-256 与运行前记录一致；
- 公开摘要不含宿主绝对路径、Trace、workspace、API Key 或 Base URL；
- `runs/` 和原有 `logs/` 均未加入 Git。

## 稳定验证

```text
pytest                                      753 passed
ruff check src tests                        passed
ruff format --check src tests               138 files already formatted
python -m compileall -q src tests            passed
uv build --no-config                        wheel + sdist passed
最终 90/12/3 身份、成功数与成本断言          passed
公开摘要路径/Trace/Key/URL 脱敏检查          passed
常见真实凭据前缀与新增 URL 扫描               0 match
git diff --check                            passed
```

`runs/` 与 `dist/` 由 `.gitignore` 排除；用户原有未跟踪 `logs/` 保持不变且不加入提交。

## 尚未完成

- 从干净工作区执行冻结 420 秒协议的完整 90-trial 回归；
- 实现冻结后一次性加入至少 3 个新 holdout 任务并执行至少 27 个 trial；
- 以上两项完成后再做阶段 3 最终覆盖率与发布级总验证。

因此本迭代可以证明诊断续跑、链式成本合并和失败追踪链路成立。当前工程里程碑在 88/90 派生
视图处收尾；完整同协议 90-trial 和新任务 holdout 作为后续评测保留，不能把 88/90 写成替代
78/90 的新冻结基线。
