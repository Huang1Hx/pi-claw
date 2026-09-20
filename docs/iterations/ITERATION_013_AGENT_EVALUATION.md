# Iteration 013：受限真实 Agent 评测入口

日期：2026-09-16。分支：`refactor/package-layout`。状态：`待用户审核`。
本文件记录评测实现与验证结果。

## 代码修改

| 文件 | 变更 |
|---|---|
| `src/evaluation/tasks.py` | 只读诊断、实际修复两类任务；初始代码、参考实现、公开测试及外部行为用例 |
| `src/evaluation/grading.py` | 有界候选检查、独立结果评分、参考/错误变体校准、重复试验指标 |
| `src/evaluation/live.py` | opt-in 真实模型调用、独立子进程/工作区、墙钟期限、有限授权、脱敏产物 |
| `tests/test_live_eval.py` | 新增 12 项评分器、执行限制、测试防篡改、重复指标回归 |
| `docs/EVALUATION.md` | 增加评测运行入口与范围说明 |

没有修改 Agent、Provider、路由或宿主验收逻辑；没有改真实 Key、URL 或角色配置。
评分器固定后开始本批试验，运行期间未更改评分逻辑。

## 验证记录

- 全量 pytest：456 项通过；Ruff、格式、编译、uv lock 检查通过。
- 工程 Eval 8/8；路由合约 6/6；均为离线替身验证。
- 评分器校准：参考通过，原始漏洞及拒绝所有输入的错误变体失败；公开 unittest 4 项通过。
- wheel/sdist 构建通过；源码目录外安装 wheel 后可独立运行评分器校准，不调用模型。
- 真实固定 primary 试验：2 任务 × 2 次，严格任务通过 2/4。
- 原始结果：`runs/eval-20260916-primary/report.json`，各试验目录保留轨迹、最终代码和评分。
- 服务端已报告 Token 共 24,208；1 次超时的用量未知，不能当作零消费。

运行命令与安全边界见 `docs/EVALUATION.md`。
真实试验采用受限纯函数任务与单独预算，不代表生产认证安全或通用 Coding Agent 成绩。
未新增暂存、commit、合并或 push；保留全部前序修改与原有 README 大小写重命名暂存。
