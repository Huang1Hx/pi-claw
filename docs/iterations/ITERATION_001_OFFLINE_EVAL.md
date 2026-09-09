# Iteration 001：可量化离线评测基线

## 状态

`已提交 optimize`。2026-09-10 用户授权提交，随 Iteration 002/003 一并提交至本地 `optimize`；未合并 `main`、未 push。测试数据保留本轮历史快照。

## 基线与目标

优化前项目已有 Agent Loop、会话恢复、Policy、审批、Trace 和自动化测试，但缺少一条可以固定
任务输入、执行真实工具链并输出机器可读指标的回归入口。本轮建立版本化、可失败、可回放的离线
评测基线，用于验证执行层，而不是评价真实模型规划能力。

## 范围

1. `pi-claw eval` CLI：运行完整 suite 或筛选指定 case。
2. 版本化 JSON suite：固定任务、fixture、Provider 动作、Policy 和验收断言。
3. 真实执行链：复用 Agent、Policy、ToolExecutor 和 TraceWriter，不复制简化执行器。
4. 上下文断言：`require_last_tool_result` 验证 ToolResult 已进入下一轮 Provider 输入。
5. 原子 JSON 报告与每 case 独立脱敏 JSONL Trace。

## 修改文件

| 文件 | 类型 | 修改内容 |
|---|---|---|
| `src/pi_claw/eval.py` | 新增 | suite 加载、case 执行、证据断言、聚合指标、原子报告写入 |
| `src/pi_claw/eval_cases.json` | 新增 | 5 个版本化离线场景及预期证据 |
| `src/pi_claw/cli.py` | 修改 | 增加 `eval` 命令和报告输出 |
| `src/pi_claw/__init__.py` | 修改 | 导出 Eval 公共接口 |
| `tests/test_eval.py` | 新增 | 默认 suite、失败检测、筛选、非法路径和报告测试 |
| `tests/test_cli_e2e.py` | 修改 | 完整 CLI、单 case 和错误输入验收 |
| `docs/EVALUATION.md` | 新增 | 场景、指标、扩展方法和能力边界 |
| `README.md`、架构与范围文档 | 修改 | 增加 Eval 入口和模块关系 |

## 五个初始场景

| Case | 验证内容 |
|---|---|
| `fix_and_verify` | 读取错误实现、精确替换、运行 unittest、检查最终文件 |
| `recover_after_test_failure` | 首次测试失败进入下一轮上下文，修改后复测成功 |
| `deny_unapproved_write` | 默认只读时合法路径写入仍因未审批被拒绝 |
| `deny_path_escape` | 即使自动批准，`..` 越界仍由 Policy 硬拒绝 |
| `safe_max_steps` | Provider 不结束时按最大步数安全停止 |

## 关键设计取舍

- `ScriptedProvider` 只固定模型动作，Policy、文件操作、测试子进程和 Trace 仍走生产代码路径。
- 预期失败测试和安全拒绝可以是正确证据；case 是否通过由完整断言决定，而不是要求所有
  `ToolResult.ok` 都为 true。
- fixture 路径必须是安全相对路径，拒绝绝对路径、`.` 和 `..`。
- 报告同时记录 `provider=scripted` 与 `network_used=false`，避免离线指标被误解为真实模型结果。

## 测试证据

本轮结束时默认 suite v1 为 5/5，Security 2/2；完整自动化门禁为 190 tests passed，总覆盖率
76%，`eval.py` 覆盖率 85%。这些数字属于本轮历史快照，后续迭代以各自说明书为准。

## 技术价值

评测把“某次演示成功”转换为可版本化、可报红、可回放的工程证据，并为后续上下文压缩、并发、
真实 Provider 和安全策略改动提供统一回归入口。

## 风险与遗留项

- 初始 suite 只有 5 个小型 Python case，不能代表大型仓库。
- ScriptedProvider 不能衡量真实模型规划成功率、token 成本和延迟。
- 初始版本顺序执行 case，尚未量化并发和任务隔离。

## 用户审核清单

- [ ] case 是否覆盖正常、恢复、安全和熔断路径？
- [ ] 指标名称是否准确反映离线工程基线？
- [ ] 报告和 Trace 是否足以定位失败？
- [ ] 审核通过前保持不 commit、不合并、不 push。
