# Iteration 015：三种 Agent 架构同题对照

日期：2026-09-21。分支：`refactor/package-layout`。状态：`待用户审核`。
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

- 专项 Ruff 通过；协调器和模式评分 11 项测试通过。
- 参考实现全部通过，两个带缺陷初始实现均被隐藏用例检出。
- 首批实测在规划阶段收到 HTTP 403；接口返回的当前授权模型不含 `qwen3.7-plus`。改用
  `qwen3.8-reasoner` 后又多次遇到 120 秒读取超时。用户要求停止评测，当时自动角色收敛为
  `deepseek-flash`（编码）和 `glm-5.3-flash`（分类、规划、审查），失败批次不纳入架构成绩。
- 配置调整后分别最小调用 `deepseek-flash` 与 `glm-5.3-flash`：两者均返回非空正文、无工具调用，
  服务端分别报告 91 / 142 Token；没有输出 Key 或 URL。
- 上述 GLM 分工是 Iteration 015 的历史验证记录。Iteration 016 因后续 GLM 调用持续失败，已把
  当前普通角色统一切换为学校 `deepseek-flash`，仅在高风险审查时使用独立 `kimi-k3`。
- grader 1.2 修正安全门误拒纯辅助函数和 `sort(key=lambda ...)` 的问题，仍限制目标签名、函数数、
  递归、顶层执行和调用白名单；共同任务提示显式写出语法边界。增加订单/窗口容器等隐藏边界。
  期限中断现在恢复各参与者 Trace 中的已知 Token 并标记未知在途量。
- 被旧门槛拒绝的两个窗口实现经 1.2 只读复核均通过 12/12 行为检查，旧批次不作为正式成绩。
- 全量 pytest 499 项通过；Ruff、格式、diff 空白检查和 wheel/sdist 构建通过。
- 前次部分产物仅保留作故障证据，不混入结果；新 12-trial 对照使用全新目录。
- 正式批次 `runs/agent-modes-final-20260921/` 完成 12/12 trial：单 Agent 2/4、顺序工作流
  0/4、主从 2/4；pass@2 分别 50%/0%/100%，pass² 分别 50%/0%/0%。
- 已知 Token 分别为 42,316 / 13,732 / 43,456；每严格成功下界为 21,158 / 不适用 /
  21,728。中位耗时分别为 132.005 / 140.405 / 264.207 秒。
- 三种模式均为 0 越权、0 写冲突、0 返工。共 15 个超时请求没有服务端 usage，报告将单 Agent
  1 个、工作流 4 个、主从 10 个缺口显式计入，不把已知 99,504 Token 冒充精确总量。
- 执行结束原始汇总保留为 `report-execution.json`；`report.json` 只修正 usage 缺口核算，
  严格成功与文件/Trace 评分不变，并同时记录执行源码和报告源码指纹。

## Git 与遗留项

未暂存、commit、合并或 push；保留此前工作区修改和 README 大小写重命名状态。
真实报告保存在 Git 忽略的 `runs/`，不保存 API Key 或 Base URL。
