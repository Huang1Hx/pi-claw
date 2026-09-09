# Iteration 002：消息流、锁、任务隔离与本地并发

## 状态

`已提交 optimize`。2026-09-10 用户授权提交，随 Iteration 001/003 一并提交至本地 `optimize`；未合并 `main`、未 push。测试数据保留本轮历史快照。

## 基线与目标

Iteration 001 已提供 5 个确定性离线 case，但只顺序执行，文档也没有把消息持久化、权限、锁和
任务隔离放在同一张边界图中说明。Iteration 002 的目标是：

1. 沿一条完整消息链解释 Agent 如何根据 ToolResult 继续决策；
2. 纠正“锁控制权限”的混淆，明确 Policy、锁和隔离各自职责；
3. 在不引入 Docker、远程服务或第三方依赖的前提下实现可测的本机并发 Eval；
4. 给出分布式 Coordinator/Worker 协议和量化指标，但不虚构多机实现。

## 本轮修改

| 文件 | 类型 | 修改内容 |
|---|---|---|
| `src/pi_claw/eval.py` | 修改 | 线程池并发、独立 `isolation_id`、并发/隔离/速度聚合指标 |
| `src/pi_claw/eval_cases.json` | 修改 | suite v2；新增 A/B 同名文件隔离 case |
| `src/pi_claw/cli.py` | 修改 | `eval --workers` 与终端并发摘要 |
| `tests/test_eval.py` | 修改 | 并发、隔离 ID、非法 worker 数测试 |
| `tests/test_cli_e2e.py` | 修改 | 3 workers 完整 CLI 和 7 条 Trace 验收 |
| `docs/MESSAGE_FLOW_LOCKS_AND_CONCURRENCY.md` | 新增 | 消息序列、三种持久化、锁临界区、权限、隔离和分布式协议 |
| `docs/EVALUATION.md` | 修改 | suite v2 场景与指标公式 |
| `docs/ARCHITECTURE.md` | 修改 | 本地并发数据流与准确能力边界 |
| `docs/PROJECT_SCOPE.md` | 修改 | v1.2 范围和分布式非目标 |
| `README.md` | 修改 | 并发快速入口与核心文档链接 |

## 设计取舍

### 1. 锁不承担授权

权限由 Policy/Approval 决定；Workspace/Session 提供任务边界；Session `flock` 和
`expected_seq` 只维护同一会话的并发一致性。这样越权、跨任务串话和 stale write 可以分别测试。

### 2. 当前实现本机任务并发，不冒充分布式

Eval 使用 `ThreadPoolExecutor`，每个 case 拥有独立的 Workspace、Agent、Provider、Policy、
ToolExecutor 和 Trace。它适合文件/子进程型评测，不用于 CPU 密集推理，也没有跨机器队列。

### 3. 速度只是观察指标

报告记录 `wall_duration_ms`、`serial_work_ms`、`observed_speedup` 和 `parallel_efficiency`，但测试
不设置固定 speedup 门槛。机器负载会影响墙钟时间；可靠门禁是 case 全通过、确实出现并发和
`task_isolation_violations=0`。

## 分布式扩展边界

文档定义了 Coordinator、持久队列、独立 Worker workspace、lease、heartbeat、幂等键和 fencing
token。当前代码没有这些组件，也没有远程故障注入，因此只能作为下一阶段技术设计说明。

## 验收命令

```bash
uv sync --locked
uv lock --check
uv run ruff format --check src tests
uv run ruff check src tests
uv run python -m compileall -q src/pi_claw
uv run pytest -q

uv run pi-claw eval --workers 1 --output <串行报告> --traces-dir <串行轨迹目录>
uv run pi-claw eval --workers 4 --output <并发报告> --traces-dir <并发轨迹目录>
git diff --check
```

## 测试证据

本轮实测默认 suite v2：

| 模式 | 结果 | 最大同时运行 | 隔离违规 | 墙钟时间 | case 耗时之和 | 观察加速比 | 并行效率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `--workers 1` | 7/7 | 1 | 0 | 85.444 ms | 85.250 ms | 0.9977 | 0.9977 |
| `--workers 4` | 7/7 | 4 | 0 | 53.020 ms | 105.421 ms | 1.9883 | 0.4971 |

这是 2026-08-12 在当前 Mac 上的一次快速样本，只证明指标可采集、并发发生且隔离断言通过，
不承诺稳定获得约 1.99 倍加速。完整 uv 门禁结果：

- `uv sync --locked`：通过；
- `uv lock --check`：通过；
- `ruff format --check`、`ruff check`：通过；
- `compileall`：通过；
- `pytest`：195 passed；总覆盖率 76%，`eval.py` 覆盖率 86%；
- `uv build`：sdist 与 wheel 均构建成功；
- 串行/4-worker Eval：均为 7/7，Security 2/2，隔离违规 0；
- `git diff --check`：通过。

## 技术价值

本轮把本机并发离线 Eval 和独立 Workspace 隔离变成可重复验证的工程能力，并输出最大并行数、
隔离违规、墙钟时间、观察加速比和并行效率。分布式部分只保留 lease/fencing 扩展设计。

## 遗留项

- 真实模型并发任务的成功率、token 成本和限流行为尚未测试；
- 没有跨机器 Coordinator/Worker、持久队列和远程结果存储；
- 没有 Docker/OS Sandbox，安装和启用仍由用户决定；
- suite 规模仍小，速度值不能当作通用性能基准。

## 用户审核清单

- [ ] 是否接受“锁不控制权限”的术语纠正？
- [ ] 是否接受只实现本机并发、分布式保留为设计？
- [ ] JSON 指标是否足以支持工程回归分析？
- [ ] 是否继续补真实 Kimi/Qwen/DeepSeek 独立评测矩阵？
- [ ] 审核通过前保持不 commit、不合并、不 push。
