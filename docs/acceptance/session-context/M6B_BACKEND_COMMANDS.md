# M6b 验收：后端切换、迁移接线与观测补齐

- 阶段：M6b（files / sqlite 后端配置、入口接线、迁移命令与真实落库核对）
- 日期：2026-10-14
- 前置：M5 ACCEPTED
- 主计划：`newplan.md` §8(M6b)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 用单一工厂收敛后端选择：`显式参数 > PI_CLAW_SESSION_BACKEND > 默认 files`；未知取值显式报错。
- 全部实际入口（`chat`、`sessions`、`migrate`）与 REPL 装配只依赖 `SessionRepository`。
- `/context`、`/status` 如实报告**实际**会话后端，用户不再需要猜“数据写进了哪里”。
- 提供显式 `migrate` 命令并在失败时不触碰源数据；补 `session_migrated` 等后端 Trace 事件（仅安全元数据）。
- **真实 REPL/CLI 会话落到 SQLite 后，用独立 sqlite3 连接直读原始行核对**，不靠同源 API 自证。

**非目标**

- 不做常驻双向同步或自动清理旧后端；不做综合评测（M7b）；不新增外部服务、向量库或语义记忆。

## 2. 设计要点

- 新增 `src/persistence/factory.py`：
  - `resolve_backend(backend, env=...)`：显式参数优先于环境变量，空值视为未设置，非法值 `ValueError`。
  - `open_session_store(data_dir, backend=...)`：`files → SessionStore`；`sqlite → <data_dir>/sessions.sqlite3`。
  - `store_backend(store)`：识别实例后端，供命令与 Trace 如实报告。
- 故障语义：SQLite 构造失败（如数据目录被同名文件占用）直接抛 `OSError`，**绝不回退 files**，
  避免“以为写进 SQLite、实际留在文件后端”的静默分叉。
- CLI：`chat`/`sessions` 增 `--session-backend`；新增 `migrate --workspace --from --to [--data-dir] [--trace]`。
- Trace：`session_migrated`（`from_backend/to_backend/total/imported/skipped`）、
  `session_migration_failed`（`reason=conflict`）；不含消息正文或凭据。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试/命令 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| M6b-1 | 默认后端为 files；env 选 sqlite；显式参数覆盖 env | `test_backend_factory.py::test_default_backend_is_files` / `::test_env_selects_sqlite` / `::test_explicit_backend_overrides_env` | passed | PASS |
| M6b-2 | 未知后端显式报错，不静默回退 | `::test_unknown_backend_rejected` | passed | PASS |
| M6b-3 | SQLite 库文件位于数据目录单文件；失败不回退 files | `::test_sqlite_file_lives_under_data_dir` / `::test_sqlite_failure_never_falls_back_to_files` | passed | PASS |
| M6b-4 | 双后端 `/context` 报告实际后端 | `test_backend_commands.py::test_context_reports_actual_backend[files|sqlite]` | passed | PASS |
| M6b-5 | 双后端 `/pin`+`/goal` 跨重载持久化 | `::test_pin_and_goal_persist_across_reload[files|sqlite]` | passed | PASS |
| M6b-6 | CLI `--session-backend` 覆盖环境变量 | `::test_cli_sessions_flag_overrides_env` | passed | PASS |
| M6b-7 | `migrate` 迁移成功、源不变、写 `session_migrated` | `::test_cli_migrate_and_trace` | passed | PASS |
| M6b-8 | 迁移冲突失败、目标不被部分接受、写失败事件 | `::test_migration_conflict_fails_without_partial_acceptance` | passed | PASS |
| M6b-9 | 真实 REPL 落 SQLite，独立连接核对（脱敏/水位线/integrity） | `::test_real_repl_sqlite_rows_are_independently_readable` | passed | PASS |
| M6b-10 | 真实 CLI 进程（env=sqlite）+ 独立连接核对 | `::test_cli_chat_sqlite_session_independently_readable` | passed | PASS |
| M6b-11 | 全量回归、Ruff、格式 | 见 §5 | — | PASS |

## 4. 真实落库证据（独立连接）

真实 CLI 会话（`PI_CLAW_SESSION_BACKEND=sqlite`，隔离数据目录）：

```
$ PI_CLAW_SESSION_BACKEND=sqlite python -m interfaces chat --provider scripted --print \
    --data-dir /tmp/m6b-real/sessions "列出项目结构"
$ python - <<'PY'   # 独立 sqlite3 连接，不经 SessionRepository
... 读 snapshots.context / events.MAX(seq) / PRAGMA integrity_check ...
PY
tables: ['meta', 'sessions', 'events', 'snapshots', 'usage_ledger']
integrity: ok
snapshot: fa2ddb56… through_seq=5 max_seq=5 messages=5 v=3
-rw------- sessions.sqlite3   # 0600，同组/其他用户不可读写
```

`through_seq == MAX(seq)`、快照为 v3、`integrity_check=ok`、库文件 0600 均成立；pins 中注入的
`sk-…` 形态凭据在原始行中被脱敏（`test_real_repl_sqlite_rows_are_independently_readable`）。

## 5. 证据与结论

- 专项：`tests/test_backend_factory.py`（8）+ `tests/test_backend_commands.py`（9）→ `17 passed`。
- 相邻回归：`test_pin_commands.py`、`test_migration.py`、`test_sqlite_repository.py`、
  `test_context.py`、`test_session.py`、`test_cli_config.py` → 全绿。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制：ps/网络）, 1017 passed, 3 skipped`。
- Ruff / 格式：通过。

**结论：ACCEPTED**（M6b-A 满足：双后端配置/新建/恢复/迁移全链路通过；Trace 无凭据或正文；
故障不报成功；files 默认与既有命令无回归）。

## 6. 自审记录（两次）

**自审 #1（优先级与故障语义）**

- 复核：`resolve_backend` 对空字符串/空白按未设置处理；`open_session_store` 在构造前解析后端，
  未知值在进入任何 I/O 之前报错；SQLite 数据目录被文件占用时抛 `OSError`，验证无回退。
- 发现（已修）：测试初版用 `session_snapshots`/`messages` 列名核对，与实现表名
  (`snapshots`/`context`) 不符；已改为对独立连接可见的真实表名与字段。

**自审 #2（接线完整性与诚实性）**

- 复核：`cli.py` 已无直接 `SessionStore` new；`repl.py` 注解放宽到协议；`/context`、`/status`
  简明与 `--verbose` 均报后端；`migrate` 的 `finally` 关闭 sqlite 连接，失败路径写 Trace 并返回 2。
- 诚实性：明确写出“迁移为一次性、无自动切换与双向同步；SQLite 仍为内嵌单文件”；
  真实落库核对使用独立连接而非同源 API。
- 遗留（非缺陷）：未做迁移可视化进度；多会话大规模分批迁移留待确有需要时再评估（见 M5 §4）。
