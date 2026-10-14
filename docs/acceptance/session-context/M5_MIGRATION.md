# M5 验收：迁移和一致性

- 阶段：M5（files ↔ sqlite 迁移与一致性）
- 日期：2026-10-14
- 前置：M4 ACCEPTED
- 主计划：`newplan.md` §8(M5)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 在 files 与 sqlite 之间迁移会话，保留规范化业务内容：消息、活动上下文（pins/锚/goal/水位线/修订号）、
  用量账本（按 run_id）、会话元数据（含 `session_id`、`created_at`）。
- 明确失败语义：目标冲突在写入前报错；源数据损坏显式失败；中断可重跑且幂等。

**非目标**

- 后端工厂、入口接线与自动切换（M6b）、综合评测（M7b）；不做常驻双向同步（一次性迁移）。

## 2. 设计要点

- 新增仓储能力（`SessionRepository` 协议 + 两后端）：
  - `usage_records()`：返回按 `run_id` 分组的原始账本，保留幂等键。
  - `import_session(info, messages, context, usage) -> bool`：保留 `session_id`/`created_at`；
    同名同 `created_at` 幂等返回 `False`，同名不同 `created_at` 显式冲突。
- 新增 `src/persistence/migrate.py`：`migrate_workspace(source, target, workspace)`。
  - **两阶段**：先读全量并预检目标冲突（不写），再逐个导入。
  - 文件后端导入先写隐藏临时目录再 `os.rename` 原子落地；SQLite 导入在单个事务内完成。
  - 中断只产生“已完成 / 未开始”，重复运行幂等补齐。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| M5-1 | files→sqlite 规范化内容往返相等 | `test_files_to_sqlite_round_trip_equality` | passed | PASS |
| M5-2 | 重复导入幂等：无重复事件/Usage | `test_repeat_migration_is_idempotent` | passed | PASS |
| M5-3 | sqlite→files 反向迁移相等 | `test_reverse_migration_sqlite_to_files` | passed | PASS |
| M5-4 | 目标冲突在写入前报错，目标不变 | `test_conflict_reports_before_any_write` | passed | PASS |
| M5-5 | 中断后源完好、已导入完整、可续跑 | `test_interrupted_migration_is_resumable` | passed | PASS |
| M5-6 | 源快照损坏显式失败，且不触碰目标 | `test_corrupt_source_fails_without_touching_target` | passed | PASS |
| M5-7 | 双后端满足扩展后的协议（含新方法） | `test_store_implements_repository_protocol[files|sqlite]` | passed | PASS |
| M5-8 | 全量回归与静态检查 | 见 §5 | — | PASS |

## 4. 边界与如实说明

- 一次迁移会把**一个工作区**的会话整体读入内存后导入；大规模数据集的分批/流式未实现（当前规模可接受）。
- 迁移**不重算** Token、不改评分、不修改源；`updated_at` 随导入保留自源。
- 不做常驻双向同步，也不处理“迁移后又各自写入”的合并；切换语义由 M6b 负责。

## 5. 证据与结论

- 专项：`tests/test_migration.py` → `6 passed`（双向、幂等、冲突、中断、损坏源）。
- 契约/后端：`test_session_repository_contract.py`（双后端 14）、`test_sqlite_repository.py`（9）、
  `test_sqlite_concurrency.py`（6）。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 1000 passed, 3 skipped`。
- Ruff/格式：通过。

**结论：ACCEPTED**（M5-A 满足；入口接线与真实 REPL 落库见 M6b）。

## 6. 自审记录（两次）

**自审 #1（实现与原子性）**

- 复核：预检在任何写入前完成；文件后端用隐藏临时目录 + `os.rename` 保证不产生半成品会话；
  SQLite 导入为单事务；`import_session` 幂等与冲突语义两后端一致。
- 发现（已修）：中断可恢复性测试最初假定导入顺序，实际 `list_sessions` 按 `updated_at` 倒序；
  已改为顺序无关断言。
- 发现（非缺陷）：`import_session` 在两后端都接受空 `messages`（用于构造对照会话），
  读取时退化为“仅快照”，符合契约。

**自审 #2（失败路径与诚实性）**

- 增补失败路径：源快照版本损坏 → 显式失败且目标零写入（M5-6）。
- 诚实性：明确写出“非双向同步、内存整体读取、不重算 Token”。
- 遗留（非缺陷）：未做迁移过程的可视化进度与回滚脚本，M6b 提供显式切换与故障反馈。
