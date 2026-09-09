# Pi-Claw 迭代说明书

## 记录规则

每轮优化都新增一份独立说明书，不覆盖旧记录。文件命名为：

```text
ITERATION_<三位序号>_<主题>.md
```

每份说明书至少包含：基线、目标、范围、修改文件、设计取舍、测试证据、技术价值、风险、遗留项
和 Git 状态。状态只使用：

- `开发中`：本轮仍在修改；
- `待用户审核`：代码和测试已完成，但没有用户批准；
- `已批准待提交`：用户明确通过，尚未提交；
- `已提交 optimize`：只提交到优化分支；
- `已合并 main`：用户再次批准后才允许合并；
- `已推送远程`：用户明确批准后才允许 push。

## 迭代索引

| 迭代 | 主题 | 状态 | 核心产物 |
|---|---|---|---|
| [Iteration 001](ITERATION_001_OFFLINE_EVAL.md) | 可量化离线评测基线 | 已提交 optimize | `pi-claw eval`、5 个 case、JSON 报告 |
| [Iteration 002](ITERATION_002_MESSAGE_LOCKS_CONCURRENCY.md) | 消息流、锁、隔离与本地并发 | 已提交 optimize | 7 个 case、并发指标、分布式扩展设计 |
| [Iteration 003](ITERATION_003_VERIFIED_COMPLETION.md) | 工作流梳理与可验证完成 | 已提交 optimize | 宿主完成验收、`unverified`、上游对照 |

2026-09-10：用户明确授权 `git commit`，Iteration 001–003 合并为一次本地 `optimize` 提交。
后续合并 `main` 或推送远程按用户指令执行。
