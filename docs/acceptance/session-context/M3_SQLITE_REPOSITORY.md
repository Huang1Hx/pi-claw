# M3 验收：SQLite SessionRepository

- 阶段：M3（SQLite 会话仓库）
- 日期：2026-10-14
- 前置：M7a ACCEPTED（第一批已验收）
- 主计划：`newplan.md` §8(M3)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 在不改上下文压缩、脱敏、权限与审批的前提下，用 SQLite 实现与文件后端**同一契约**的 `SessionRepository`。
- 契约测试参数化为 files/sqlite，两条实现共享同一组用例。
- 会话事件、完整活动快照、元数据与用量具备**明确原子提交**与统一错误语义。

**非目标**

- 并发/崩溃耐久（M4）、迁移（M5）、工厂与入口接线（M6b）、综合评测（M7b）。
- 不引入外部服务、Redis、向量库；SQLite 仍是内嵌单文件。

## 2. 设计要点

- 新增 `src/persistence/sqlite_repository.py`：`SqliteSessionRepository`。
- 介质映射：`context.jsonl`→`events`；`active_context.json`→`snapshots`（存 `to_payload()` JSON）；
  `state.json`→`sessions`；`usage.json`→`usage_ledger`。
- 一致性：`BEGIN IMMEDIATE` 写事务 + `COMMIT/ROLLBACK`；`WAL` + `synchronous=FULL`；
  写事务内先校验快照可解析（fail-closed），再改事件/快照，整体原子。
- 语义等价：`expected_seq` 不匹配 → `RuntimeError("另一个进程…")`；`expected_revision` 为可选 CAS；
  未知快照版本 → `ValueError`；工作区不符 → `ValueError`；凭据入 URL → `ValueError`。
- 复用：`Workspace` 校验真实路径、`redact` 脱敏、`SessionStore._validate_runtime_metadata`、
  `CheckpointMixin._repair_incomplete_tool_calls`、`ActiveContext/Pin/TaskAnchor` 序列化。
- 数据库文件 `chmod 0600`；构造时幂等建表并写入 `schema_version`。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| M3-1 | files/sqlite 均满足 `SessionRepository` 协议 | `test_store_implements_repository_protocol[files|sqlite]` | passed | PASS |
| M3-2 | 创建/追加/加载/列表语义等价 | `test_create_append_load_and_list`（双后端） | passed | PASS |
| M3-3 | 过期写入者被拒绝，不静默覆盖 | `test_stale_writer_is_rejected`（双后端） | passed | PASS |
| M3-4 | 完整快照 pins/anchor 往返等价 | `test_active_context_round_trips_pins_and_anchor`（双后端） | passed | PASS |
| M3-5 | 消息兼容保存不擦除 pins/anchor | `test_message_save_keeps_pins_and_anchor`（双后端） | passed | PASS |
| M3-6 | 用量按 run_id 幂等、不再重复累计 | `test_usage_is_idempotent_per_run`（双后端） | passed | PASS |
| M3-7 | 标题/审批/runtime 元数据等价 | `test_update_state_and_runtime`（双后端） | passed | PASS |
| M3-8 | 追加失败整体回滚，无半条事件 | `test_append_failure_rolls_back_without_partial_event` | passed | PASS |
| M3-9 | 工作区隔离：列表/加载/记账不跨读 | `test_workspace_isolation` | passed | PASS |
| M3-10 | 修订号 CAS 拒绝过期原地更新 | `test_revision_cas_rejects_stale_snapshot_update` | passed | PASS |
| M3-11 | 关闭重开后事件/快照/用量完整可见 | `test_state_survives_new_instance` | passed | PASS |
| M3-12 | 未知快照版本 fail-closed 且不写入 | `test_unknown_snapshot_version_fails_closed_without_write` | passed | PASS |
| M3-13 | runtime URL 拒绝凭据/查询参数 | `test_update_runtime_rejects_credentials_in_url` | passed | PASS |
| M3-14 | 用量跨会话聚合；goal/anchor 保存保留 | `test_usage_is_idempotent_and_aggregates`、`test_snapshot_save_preserves_goal_and_anchor` | passed | PASS |
| M3-15 | 全量回归与静态检查 | 见 §5 | — | PASS |

## 4. 语义等价边界（如实记录）

- 文件后端按“单机文件锁 + 分步落盘”提交；SQLite 用单事务提交，等价或更强，但**锁粒度不同**
  （SQLite 为整库写锁）。单用户本地 CLI 无影响；跨会话完全并行留待 M4 评估。
- `-wal`/`-shm` 由 SQLite 按 umask 创建，未显式收紧；库文件本身为 `0600`。M4 补充资源与权限检查。

## 5. 证据与结论

- 契约（双后端）：`tests/test_session_repository_contract.py` → `14 passed`（files 与 sqlite 各 7）。
- SQLite 专项：`tests/test_sqlite_repository.py` → `8 passed`。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 987 passed, 3 skipped`。
- Ruff/格式：`ruff check src tests`、`ruff format --check src tests` 通过。

**结论：ACCEPTED**（M3-A 满足；并发/崩溃耐久与迁移分别留待 M4、M5）。

## 6. 自审记录（两次）

**自审 #1（实现与事务边界）**

- 发现 1（已修，真实缺陷）：`append_message` 未传 `active_messages` 时只把 `existing.messages` 写入新水位线，
  跳过了本次尾部消息。已改为合并 `through_seq` 之后的 journal 尾部，避免“水位线前移却丢尾”。
- 发现 2（已修）：`executescript` 会隐式提交，包在 `BEGIN IMMEDIATE` 中导致 “no transaction is active”。
  已把建表移出显式事务。
- 发现 3（已修）：`append_message` 冗余双重 `json.dumps`；读取路径未按 `max_bytes` 限制。已简化并补齐
  事件/活动上下文读取的字节预算。
- 复核点：写事务内先 fail-closed 校验快照再改数据；CAS 与 `expected_seq` 语义与文件后端一致。

**自审 #2（契约一致性与文档）**

- 逐条核对：契约测试 `BACKENDS` 扩展后无改写用例，即“同一组用例两后端等价”；去掉了
  `update_state_and_runtime` 原先的 files-only 跳过，sqlite 也纳入。
- 诚实性：如实标注锁粒度差异与 `-wal`/`-shm` 权限未收紧，未把 M4/M5 能力写成已具备。
- 遗留（非缺陷）：外键 `ON DELETE CASCADE` 已声明，但删除会话的产品路径尚未实现（本批无该需求）。
