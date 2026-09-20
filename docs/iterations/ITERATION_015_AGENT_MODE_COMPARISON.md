# Iteration 015：三种 Agent 架构同题对照

日期：2026-09-20。分支：`refactor/package-layout`。状态：`开发中`。
仅记录产品代码、评测实现和验证结果，不加入面试或简历材料。

## 基线、目标与范围

基线为 Iteration 014 的单 Agent 真实模型评测和顺序规划—编码—审查工作流。本轮新增单宿主
主 Agent＋独立只读子 Agent 协调器，并使用同一批任务对照三种架构。重点指标为严格成功率、
重复成功率、端到端耗时、总 Token、每个成功任务 Token、写冲突、越权和返工。

## 修改文件

| 文件 | 修改 |
|---|---|
| `src/core/team.py` | 独立子 Agent 并行只读探索、屏障、有界交接、唯一写者、主从 Token 汇总 |
| `src/evaluation/mode_tasks.py` | 两个固定中等复杂度任务、起始实现、公开测试和参考实现 |
| `src/evaluation/mode_grading.py` | 精确函数签名、有界 AST、隐藏行为、夹具与输入不变性评分 |
| `src/evaluation/compare_modes.py` | 12-trial 编排、子进程期限、三种模式、统一评分、可靠性/效率/安全指标 |
| `tests/test_team.py` | 独立实例、真实并行、上下文交接、只读权限和总 Token 回归 |
| `tests/test_agent_mode_comparison.py` | 评分校准、三模式同门槛、越权去重、pass@k/pass^k 和 Token 公式 |
| `docs/AGENT_MODE_COMPARISON.md` | 具体任务、模型分配、指标口径、命令、边界和实测结果 |
| `docs/ARCHITECTURE.md`、`docs/WORKFLOW_AND_DATA_FLOW.md`、`docs/EVALUATION.md` | 架构、数据流和评测入口同步 |

## 设计取舍与边界

- 两个子 Agent 仅做可并行的实现/风险探索；编码仍由唯一主 Agent 串行提交，避免多写者合并噪声。
- 每个参与者必须是独立 Agent、Provider、上下文和 Trace；禁止并发复用单 Agent 实例。
- 子 Agent Policy 禁止命令和写入；交接产物是有界不可信数据，不传递私有推理或完整上下文。
- 三模式共享主任务提示、工作区夹具、公开命令、隐藏评分和 trial 预算；工作流返工属于架构成本。
- 总 Token 包含主 Agent、子 Agent、规划/编码/审查和返工；usage 缺失不按零处理。不计算费用。
- 当前是单宿主线程并发，不实现分布式队列、租约、跨机锁或故障接管，也不宣称通用代码沙箱。
- 仅两个局部纯函数任务、每任务两次重复，适合展示方法和初步差异，不足以证明通用能力排名。

## 验证记录

- 专项 Ruff 通过；协调器和模式评分 9 项测试通过。
- 参考实现全部通过，两个带缺陷初始实现均被隐藏用例检出。
- 整库测试、构建和 12 次真实模型 trial 待本轮源码冻结后执行并回填。

## Git 与遗留项

未暂存、commit、合并或 push；保留此前工作区修改和 README 大小写重命名状态。
真实报告保存在 Git 忽略的 `runs/`，不保存 API Key 或 Base URL。

