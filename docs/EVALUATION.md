# 离线评测设计

## 评测要回答什么

Pi-Claw 的评测先回答一个比“某次模型表现好不好”更基础的问题：当 Provider 给出确定动作时，
Agent Loop、Policy、审批、Tool、Trace 和终止条件能否稳定地组成闭环。

默认评测完全离线，使用版本化 `ScriptedProvider`，不访问网络、不读取 API Key，也不把模型随机性
混入工程回归。它可以证明执行层和安全边界符合预期，但不能证明真实 Kimi、Qwen、DeepSeek 或
OpenAI 模型的任务成功率。

## 一条命令运行

```bash
uv sync --locked
uv run pi-claw eval
```

默认在 `runs/` 下生成：

- 一份 JSON 总报告；
- 每个 case 一份脱敏 JSONL Trace。

指定稳定输出路径，便于演示前后比较：

```bash
uv run pi-claw eval \
  --workers 4 \
  --output runs/offline-eval.json \
  --traces-dir runs/offline-eval-traces
```

只运行一个场景：

```bash
uv run pi-claw eval --case deny_path_escape
```

## 版本化基线场景

默认任务集位于 `src/pi_claw/eval_cases.json`，当前 schema 与 suite 均有独立版本号。

| Case | 类别 | 主要证据 |
|---|---|---|
| `fix_and_verify` | Coding | 读取错误实现、最小替换、运行测试、检查最终文件 |
| `recover_after_test_failure` | Recovery | 首次测试失败、读取证据、修复、复测成功 |
| `deny_unapproved_write` | Security | 默认只读，合法路径写入也因未审批被拒绝 |
| `deny_path_escape` | Security | 即使自动批准开启，`..` 越界仍被 Policy 硬拒绝 |
| `safe_max_steps` | Reliability | Provider 不给最终答案时按最大步数安全停止 |
| `isolation_worker_a` | Isolation | 并发任务 A 只看到自己的同名文件 |
| `isolation_worker_b` | Isolation | 并发任务 B 只看到自己的同名文件 |
| `reject_unverified_completion` | Reliability | 写入后无验证便声明完成时返回 `unverified` |

Case 的通过条件不是“Agent 返回了 final_text”这么简单。每个场景可同时断言：

- 真实 `RunStatus`；
- ToolResult 的顺序、工具名、成功状态和关键错误；
- Trace 必须存在的事件；
- 工作区文件的最终内容；
- 工作区外哨兵文件保持不变。

## 报告指标定义

- `pass_rate`：满足全部预期断言的 case 比例；这是确定性离线工程基线。
- `security_pass_rate`：Security 类 case 的通过比例。
- `security_violations`：未满足预期的 Security case 数量；正常应为 0。
- `assertion_failures`：所有 case 中失败的断言数量。
- `total_tool_calls`：实际产生 ToolResult 的调用数量。
- `successful_tool_calls`：`ToolResult.ok=true` 的数量。
- `non_ok_tool_results`：失败测试、策略拒绝、审批拒绝等 `ok=false` 结果的总数。它不是项目失败数；
  在恢复和安全 case 中，非 OK 结果正是预期证据。
- `policy_denials`：被 Policy 硬拒绝的调用数量。
- `approval_denials`：进入 ASK 后因未获批准而拒绝的调用数量。
- `completion_rejections`：模型 final candidate 因缺少写入后验证证据而被宿主拒绝的次数。
- `verified_completions`：通过宿主最低证据检查的完成数。
- `unverified_runs`：步数耗尽时仍没有完成证据的运行数。
- `mean_steps`：每个 case 的平均 Agent 模型轮数。
- `task_isolation_violations`：重复匿名 Workspace ID 与重复 Trace 路径的总数，正常为 0。
- `workers_requested / workers_used`：用户请求并发数和本次实际使用的 worker 数。
- `max_parallel_cases`：同时处于执行区的最大 case 数，用于证明并发确实发生。
- `wall_duration_ms`：整批任务的墙钟时间。
- `serial_work_ms` / 兼容字段 `duration_ms`：各 case 自身耗时之和。
- `observed_speedup`：`serial_work_ms / wall_duration_ms`。
- `parallel_efficiency`：`observed_speedup / workers_used`。

速度值仅用于同机、同 suite 的回归观察，不应跨机器宣传为性能基准。并发实现、隔离边界和指标
公式见 [消息流、锁与并发](MESSAGE_FLOW_LOCKS_AND_CONCURRENCY.md)。

报告同时写入 `provider=scripted` 和 `network_used=false`，防止离线指标被误当成真实模型成绩。

## 如何增加一个评测场景

1. 在 `eval_cases.json` 增加唯一 `id`、类别、任务和临时工作区文件。
2. 用 `replies` 描述确定性的 Tool Call / final text 序列；`{{PYTHON}}` 会映射到当前 uv 环境解释器。
   可用 `require_last_tool_result` 要求下一步返回前必须在上下文看到指定工具结果、成功状态和错误片段。
3. 明确 Policy 是否开放写/命令，以及是否自动批准。
4. 在 `expect` 中写状态、工具结果、Trace 事件与文件证据。
5. 先故意写错一条期望，确认 case 会失败；再修正并执行完整测试。
6. 若修改了 suite 语义或已有 case，提高 suite `version`；若改变 JSON 结构，提高 `schema_version`。

自定义 suite 中的 fixture 只能写入评测创建的临时目录，绝对路径和 `..` 会被拒绝。评测命令仍会
执行 suite 中经过 Policy 白名单的测试代码，因此只运行项目自带或你亲自审核过的 suite。

## 真实模型评测必须分开

真实 Provider 后续应复用相同任务目标，但单独记录：模型、供应商、日期、成功率、步骤数、延迟、
费用与失败分类。不能把默认离线全通过写成“真实大模型任务成功率 100%”。
