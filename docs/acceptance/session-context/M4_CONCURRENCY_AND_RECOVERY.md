# M4 验收：并发与崩溃耐久

- 阶段：M4（仓储事务、锁与快照修订控制）
- 日期：2026-10-14
- 前置：M3 ACCEPTED
- 主计划：`newplan.md` §8(M4)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 用**真实多进程**构造两类竞争：相同日志 seq 的追加竞争、相同 seq 的仅快照竞争；冲突方必须显式失败，不得静默覆盖。
- 验证进程崩溃（含未提交与已提交两种边界）后的恢复，以及 `integrity_check`。
- 验证写锁超时、文件权限与连接/临时文件清理。

**非目标**

- 迁移（M5）、工厂与入口接线（M6b）、综合评测（M7b）。
- **不做**断电或硬件故障证明：本阶段只覆盖进程内崩溃与进程被 kill 的情形。

## 2. 设计要点

- `SqliteSessionRepository` 增补：`integrity_check()`、`_secure_files()`（库文件与 `-wal`/`-shm` 收紧为 0600）、
  可配置 `busy_timeout_ms`；写事务 `BEGIN IMMEDIATE`，以便在冲突时按超时显式失败。
- 追加竞争：`BEGIN IMMEDIATE` 串行化 + `expected_seq` 校验（与文件后端一致的 `RuntimeError("另一个进程…")`）。
- 仅快照竞争：`expected_revision` CAS（水位线不变时），冲突方 `RuntimeError`。
- 崩溃边界：WAL + `synchronous=FULL`；未提交事务在重开时回滚，已提交事务保留。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| M4-1 | 多进程同 seq 追加：单一胜者，另一个显式失败 | `test_multiprocess_append_race_single_winner` | passed | PASS |
| M4-2 | 多进程同 seq 仅快照竞争：CAS 单一胜者 | `test_multiprocess_snapshot_only_race_single_winner` | passed | PASS |
| M4-3 | 未提交事务中 kill：重开回到完整旧状态，可继续写 | `test_kill_during_uncommitted_write_rolls_back` | passed | PASS |
| M4-4 | 已提交后 kill：数据仍在，不丢已确认提交 | `test_kill_after_commit_preserves_data` | passed | PASS |
| M4-5 | integrity_check=ok；库/WAL/SHM 权限 0600 | `test_integrity_check_and_file_permissions` | passed | PASS |
| M4-6 | 写锁被占用时按 busy_timeout 显式失败，不无限阻塞 | `test_write_lock_timeout_fails_explicitly` | passed | PASS |
| M4-8 | **独立连接直读原始行**：脱敏生效、结构与水位线一致、integrity ok | `test_raw_rows_are_redacted_and_independently_readable` | passed | PASS |
| M4-7 | 全量回归与静态检查 | 见 §5 | — | PASS |

## 4. 边界与如实说明

- **进程崩溃 ≠ 断电**：kill 测试只证明“进程被杀后事务边界正确”，不代表硬件掉电下的耐久性。
- SQLite 写锁为整库级；跨会话并行写会串行。本阶段未做分库或连接池，属后续按需评估。
- 资源清理以 SQLite 连接与 WAL 生命周期为准；`close()` 由调用方或进程退出触发。

## 5. 证据与结论

- 专项：`tests/test_sqlite_concurrency.py` → `6 passed`（真实子进程）。
- 契约+专项：`tests/test_session_repository_contract.py`（双后端 14）、`tests/test_sqlite_repository.py`（9）。
- 独立核对：除 API 读回外，已用 `sqlite3` CLI 与**独立 sqlite3 连接**直接 dump 原始表，确认凭据被 `[REDACTED]`、
  `through_seq=MAX(seq)`、`integrity_check=ok`、库/WAL/SHM 权限 0600；并固化为永久回归用例 M4-8。
- 仍**未**验证的：尚无真实交互式产品会话落库（入口接线属 M6b）；未做规模/长时压力基准。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 993 passed, 3 skipped`。
- Ruff/格式：通过。

**结论：ACCEPTED**（M4-A 满足；迁移留待 M5）。

## 6. 自审记录（两次）

**自审 #1（实现）**

- 复核：`BEGIN IMMEDIATE` 保证冲突在写锁层串行；`expected_seq`/`expected_revision` 双层保护；
  `_secure_files()` 在每次事务结束后重申权限，覆盖新建的 `-wal`/`-shm`。
- 发现（非缺陷）：`sqlite3.connect(timeout=5.0)` 与 `PRAGMA busy_timeout` 都能影响等待；已用后者作为
  显式、可配置的唯一来源（构造参数 `busy_timeout_ms`），避免两处默认值漂移。
- 发现（非缺陷）：写锁超时抛 `sqlite3.OperationalError`，未包装成领域异常；这是**显式失败**，
  M6b 将在入口层转换为用户可读反馈。

**自审 #2（测试与诚实性）**

- 逐条核对：两类竞争“单一胜者”断言按真实子进程结果统计，不是模拟；崩溃用例分别覆盖未提交/已提交边界。
- 诚实性：文档明确写出“进程崩溃不等价断电”，未夸大耐久结论。
- 遗留（非缺陷）：跨会话并行写未做压力基准，M7b 的“性能”指标按需补充样本与环境说明。
