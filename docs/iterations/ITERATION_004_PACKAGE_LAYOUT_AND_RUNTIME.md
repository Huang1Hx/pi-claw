# Iteration 004：包目录与可观测运行时

## 状态

`待用户审核`。当前工作位于 `refactor/package-layout`，未合并 `main`、未提交、未 push。

## 基线与目标

Iteration 003 后已有受控工具、人工审批、Session/Trace、上下文压缩、完成验收和
离线 Eval，但主包仍是扁平文件，Provider 没有流式事件、服务端 Token 统计、有限重试和
重复调用检测。本轮目标是：

1. 按 core/execution/security/providers/persistence/evaluation/interfaces 分层；
2. 用 Pichu 公开能力作为检查表，补齐适合 Pi-Claw 边界的运行时机制；
3. 保持原有公共导入、CLI、Session 格式、Policy/Approval 和 Eval 行为；
4. 学习、课程材料退出产品仓库，不改写 Git 历史；
5. 用离线合约测试、全量回归、Eval 和安装后验证证明重构完整性。

## 目录重构

| 新目录 | 来源与职责 |
|---|---|
| `core/` | Agent、Context、Completion、Schemas；新增 Events、Hooks、Usage、Loop Detection |
| `execution/` | 从 `tools.py` 拆出 Registry、Executor、Filesystem、Search、Command、Preview |
| `security/` | Policy、Approval、Workspace，保持为所有工具的必经路径 |
| `providers/` | 协议、Scripted/Offline Provider、OpenAI-compatible HTTP、SSE 聚合 |
| `persistence/` | 从 `session.py` 拆出 Store 门面、Journal、Checkpoint、Locking、Storage、Usage |
| `evaluation/` | 从 `eval.py`/`eval_cases.json` 迁移，只复用正式 Runtime |
| `interfaces/` | CLI 与 REPL，`python -m pi_claw` 保持不变 |

根包继续导出已有 API，并新增 AgentEvent、HookBus、HookDecision、TokenUsage、
UsageTracker 和 RepetitionGuard。Eval 仍为兼容性懒加载，避免 Runtime 反向依赖评测层。

## 新增和加固的机制

### 流式事件

OpenAI-compatible Provider 解析有字节上限的 SSE，按 index 组装交错的工具参数分片。
`text_delta` 和 `tool_call_delta` 只用于可观测；必须收到正常 `finish_reason`、完整 JSON
和 `[DONE]` 才产生 `message_complete`。Agent 只执行完整 ModelReply，不执行半条工具。

### Token 统计与重试

TokenUsage 只接受服务端返回的非负整数，未返回 usage 时标记为未知，不把字符估算
充当账单。REPL 以 run_id 幂等写入 `usage.json`，支持 `/usage` 跨恢复查询。

429/500/502/503/504 和连接类失败可有限重试，401/400 和协议错误不重试。任意流分片
已发出后失败立即终止，不重放可见输出，也不重放工具副作用。

### 上下文和循环控制

只在活动窗口中裁剪较旧、较大的工具正文，保留 call id、成败、错误、退出码和预览；
append-only journal 保留原文。裁剪后仍超限时，才进入原有确定性压缩。

重复检测对工具名、标准化参数和真实结果指纹，忽略随机 call id；达到阈值时返回
`loop_detected`。成功写入视为进展并清空近期窗口。

### Hooks 和权限

Hook 只允许可信宿主显式注册，不扫描或加载用户仓库中的脚本。回调收到数据副本；
`before_tool` 可额外拒绝，但即使返回允许，仍必须通过 ToolExecutor 的 Policy/Approval。
前置 Hook 异常时失败关闭，观察者异常不重跑工具。

## 仓库材料边界

从当前分支顶端删除并原样迁移至 `/Users/huanghx/project/pi-claw-materials/`：

- `course/poi-agent一周学完agent.pdf`、`course/说明.md`；
- `learning/LEARNING_PLAN.md`、`learning/CONCEPT_CORRECTIONS.md`、
  `learning/KIMI_CLI_V1.md`。

迁移前后 SHA-256 已一致。历史 commit 仍包含旧文件；本轮不做破坏性历史改写。
项目仓库只保留产品代码、测试、中性工程文档和迭代记录。

## 中文注释

所有 `src/pi_claw/**/*.py` 都有中文模块说明。对公共类、公共函数和安全/协议关键分支
补充中文 docstring 或行内注释，不对显而易见赋值做逐行翻译。

## 设计取舍

- 不复制 Pichu 代码，只参考其公开的模块边界和能力清单。
- 不新增 LLM 摘要请求；确定性压缩不增加成本和外部依赖。
- 不把 Hooks 做成任意插件加载，不破坏已有信任边界。
- 不对同轮工具并行化，避免写入竞态；Eval 仍只并行独立工作区。
- 不实现意图/模型路由、MCP、Subagent、分布式队列或 OS Sandbox。

## 验收命令与证据

```bash
uv lock --check --no-config
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen python -m compileall -q src/pi_claw
uv run --frozen pytest --cov=pi_claw --cov-report=term-missing
uv run --frozen pi-claw eval --workers 4 --output runs/iteration-004-eval.json
uv run --frozen python -m pi_claw --help
uv run --frozen pi-claw --help
uv build --no-config
git diff --check
```

实际结果：

- Ruff format/lint、compileall、lock check、`git diff --check` 全部通过；
- `233 passed`，全包语句覆盖率 `79%`；
- Eval `8/8` case 通过，安全场景 `100%`，14 次工具调用，策略/审批各拒绝
  1 次，完成验收拒绝 1 次，`unverified` 1 次；
- Eval 请求/实际 worker `4/4`，最大并发 `4`，工作区隔离违规 `0`；
- 成功生成 sdist 和 wheel，两者都包含 `evaluation/cases.json`；
- wheel 安装到源码目录外的全新 uv 虚拟环境后，公共 API 导入成功，内置 Eval 再次
  `8/8` 通过，workers `2/2`、最大并发 `2`、隔离违规 `0`；
- `python -m pi_claw --help` 与安装脚本 `pi-claw --help` 输出一致；
- 离线 HTTP/SSE 替身覆盖分片聚合、截断、字节预算、状态码重试与部分流不重放。

本轮没有配置、读取或调用真实 API key，未做付费模型兼容性和质量验收。

## 风险和遗留项

- OpenAI-compatible 只表示 Chat Completions 协议兼容面，Kimi/Qwen/DeepSeek/OpenAI
  的实际模型 ID、SSE usage 和错误结构仍需配置 key 后分别验证；
- 流式终端显示是尚未验收的候选文本，完整回复仍会再显示一次终态；
- 重复调用是启发式检测，不能证明所有循环都无进展；
- 用量账本与消息 journal 不是跨文件 ACID 事务，进程在两次写入间终止可丢失用量；
- `uv.lock` 在本轮开始前已有未提交的 USTC 索引 URL 变更，本轮不将其冒充为重构改动。
