# Iteration 020：评测传输有限重试

## 状态

已合并并推送 `main`，功能提交为 `2db1271`。本轮没有调用真实模型，不改变模型分工、任务、
隐藏用例、420 秒 trial 上限或 Kimi 付费边界。

## 问题

底层 `OpenAICompatibleProvider` 原本已经支持暂时性错误重试，但三模式评测入口把
`max_attempts` 强制设为 1。因此学校 DeepSeek 服务偶发 503、429、连接失败或读取超时时，trial
会立即成为 `provider_error`，无法利用已有的安全重试能力。

420 秒是整个 trial 的墙钟资源上限，不是单次 API 超时。它随首版三模式评测引入，历史记录没有
给出统计公式；当前模型单请求超时为 120 秒。现有完整基线中，完成 trial 最大耗时 226.823 秒，
非 deadline 最大耗时 268.094 秒，420 秒比后者保留约 57% 余量。用户明确要求继续保持该上限。

## 设计与修改

- 普通模型每个逻辑请求最多尝试 3 次，即首次请求加两次重试。
- 指数退避为 2 秒、4 秒，单次等待最多 10 秒；合法 `Retry-After` 优先但仍受 10 秒限制。
- 只沿用既有暂时性错误集合：429、500、502、503、504、连接失败、读取超时。
- 400/401 等非暂时性错误立即失败；流式响应一旦输出任何片段就禁止重放。
- Kimi profile 的 `max_attempts=1` 优先于全局值，避免付费审查被自动重复调用。
- 父评测进程把重试参数显式传给每个 worker；trial 的 `config-snapshot.json`、批次
  `manifest.json` 和报告记录该传输协议。
- 新增 CLI 参数 `--max-attempts`、`--retry-base-delay`、`--max-retry-delay`，默认分别为
  3、2、10；420 秒 `--deadline` 默认值保持不变。

涉及文件：

| 文件 | 修改 |
|---|---|
| `src/routing/router.py` | 接收、校验并向 HTTP Provider 传递退避参数；profile 重试次数仍可覆盖全局值 |
| `src/evaluation/compare_modes.py` | 新增评测 CLI 参数、worker 透传、快照/报告记录和启动摘要 |
| `tests/test_routing.py` | 防止路由接收配置后没有传到底层 HTTP Provider |
| `tests/test_agent_mode_comparison.py` | 防止父进程与 worker 的重试协议不一致 |

## 运行方式

默认值已经是建议设置，显式写出时等价于：

```bash
uv run --frozen python -m evaluation.compare_modes \
  --live --config pi-claw.toml \
  --task dependency_order --trials 3 --workers 1 \
  --deadline 420 --max-steps 24 \
  --max-attempts 3 --retry-base-delay 2 --max-retry-delay 10 \
  --output runs/dependency-order-retry
```

最坏情况下，一个逻辑请求的三次完整 120 秒超时加 2+4 秒等待会占用约 366 秒。trial 仍可能在
420 秒被宿主终止，这是有意保留的总资源边界；重试机制提高暂时性失败恢复概率，但不承诺多阶段
工作流在连续长超时后完成。

## 验证

- 新增测试在实现前分别因路由不接受退避参数、worker 命令缺少重试参数而失败。
- 实现后路由、流式传输、工作流连接和模式评测相关测试共 97 项通过。
- 完整回归：`562 passed in 5.03s`。
- 源代码覆盖率：`86%`（5,636 statements，806 missed）。
- Ruff 检查通过，116 个文件格式检查通过，`git diff --check` 通过。
- wheel 与 sdist 构建通过；构建产物写入项目外临时目录。

## 边界

自动重试只发生在模型尚未输出内容时，不重放工具调用，不重跑整个 trial，也不会把最终失败从
严格成功率分母中删除。报告中的逻辑请求数与 HTTP 尝试次数含义不同；`usage.retries` 和
`llm_retry` Trace 事件用于观察额外尝试，缺少服务端 usage 的失败请求仍作为缺口保留。
