# M0 调用点清单（M0_CALL_SITES）

- 阶段：M0
- 日期：2026-10-14
- 代码基线：`optimize` @ `993881f`（M0 改动尚未提交）
- 主计划：`newplan.md` §5、§6、§8(M0)
- 结论：**ACCEPTED**

## 字段说明

- 触发入口：用户/宿主动作。
- 读写接口：调用的公开或内部接口。
- seq/revision：序号与修订号的来源和更新方式。
- 保留字段：写入时是否必须保留 `pins/anchor`。
- 阶段：该调用点的接线/扩展归属。

## 1. 产品调用点（必须覆盖）

| # | 文件/函数 | 触发入口 | 读写接口 | seq/revision 来源与更新 | 目标签名/适配 | 保留字段 | 回归测试 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `src/interfaces/repl.py:121` `CodingRepl.__init__` | 启动/`--session`/`--continue` | `load_active_snapshot` | seq=journal last seq；revision=快照原值（读不推进） | 不变 | 只读 | `test_session.py::test_repl_loads_explicit_session_and_latest` | M0 |
| 2 | `src/interfaces/repl.py:126` `CodingRepl.__init__` | 启动回写 | `save_active_messages` | seq=上一步返回；revision 仅原地编辑推进 | 不变 | 保留 pins/anchor | `test_active_context.py::test_store_message_save_does_not_erase_pins_or_anchor` | M0/M1 |
| 3 | `src/interfaces/repl.py:222` `CodingRepl.run_turn` | 用户回合 | `append_message` | seq=current+1（锁内）；写前预检快照 | 不变 | 保留 pins/anchor | `test_active_context.py::test_unknown_snapshot_version_fails_before_any_write` | M0 |
| 4 | `src/interfaces/repl.py:325` `CodingRepl.run_turn` | 模型返回 usage | `record_usage` | run_id 幂等；不占 seq | 不变 | 无 | `test_session_repository_contract.py::test_usage_is_idempotent_per_run` | M0 |
| 5 | `src/interfaces/repl.py:329` `CodingRepl.run_turn` | 回合结束 | `save_active_messages` | seq=上一步返回；revision 原地推进 | M1 起需物化 pins/anchor | 保留 pins/anchor | `test_context.py::test_repl_compact_only_changes_active_context_not_raw_session` | M1/M6a |
| 6 | `src/commands/sessions.py:9,14` `/new` | 斜杠命令 | `load_active_snapshot` + `save_active_messages` | 新会话 seq=1 | 不变 | 保留（新会话为空） | `test_skills_commands.py` 相关命令回归 | M6a |
| 7 | `src/commands/sessions.py:73,78` `/resume` | 斜杠命令 | `load_active_snapshot` + `save_active_messages` | seq=目标会话 last seq | 不变 | 保留 pins/anchor | `test_session.py::test_repl_loads_explicit_session_and_latest` | M6a |
| 8 | `src/commands/context.py:8` `/compact` | 斜杠命令 | `save_active_messages` | seq 不变；revision 原地 +1 | M6a 增强显示 | 保留 pins/anchor | `test_context.py::test_repl_compact_only_changes_active_context_not_raw_session` | M1/M6a |
| 9 | `src/persistence/session_store.py:77,82` `create` | 新会话 | `append_message` + `save_active_messages` | seq 0→1；revision=0（水位线推进不计数） | 不变 | 新会话为空 | `test_session.py::test_session_create_load_append_and_list` | M0 |
| 10 | `src/persistence/journal.py:73,76-77` `append_message` | 内部追加快照推进 | `_save_active_messages_unlocked` / `_precheck_snapshot_unlocked` | seq=新消息序号；先预检后写 journal | 私有签名不变 | 保留 pins/anchor | `test_session.py` 崩溃恢复组 | M0 |

## 2. 新增公开接口（暂无产品调用点）

| 接口 | 位置 | 用途 | 阶段 |
| --- | --- | --- | --- |
| `load_active_context` | `src/persistence/checkpoint.py:55` | 读取含 pins/锚/修订号的完整上下文 | M1/M2 |
| `save_active_context` | `src/persistence/checkpoint.py:165` | 保存 pins/锚（不追加消息） | M1/M2 |
| `SessionRepository` 协议 | `src/persistence/repository.py` | files/sqlite 统一契约 | M0 定义，M3 扩展实现 |

## 3. 兼容包装器与内部推进

| 位置 | 说明 | 保留字段 | 测试 |
| --- | --- | --- | --- |
| `src/persistence/checkpoint.py:16` `load_active_messages` | 兼容入口，返回消息列表 | 读不涉及 | `test_active_context.py::test_v2_snapshot_loads_through_active_context` |
| `_read_snapshot_unlocked` | v1/v2/v3 解析，未知版本 fail closed | 读不涉及 | `test_active_context.py::test_unsupported_payload_versions_are_rejected` |
| `_read_preserved_snapshot` | 写入前读回保留 pins/anchor；未知/畸形 fail closed | 保留 | `test_unknown_snapshot_version_fails_before_any_write` |
| `_save_active_context_unlocked` | 脱敏并写 v3 | 写全部字段 | `test_payload_round_trips_pins_and_anchor` |

## 4. 测试替身与测试直接调用

| 位置 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `tests/test_session.py`（多处 `monkeypatch.setattr(store, "_save_active_messages_unlocked", …)`） | 崩溃注入替身 | 私有签名保持 `(session_dir, messages, through_seq)` | M0 未改签名，替身继续可用 |
| `tests/test_runtime_features.py:198` | 直接调用 | `save_active_messages`/`load_active_messages` | 语义不变 |
| `tests/test_active_context.py` | 新契约测试 | 16 项 + M0-B 3 项 | v3/升级/降级/预检 |
| `tests/test_session_repository_contract.py` | 双后端契约（当前 files） | M3 扩展 `BACKENDS` | 端口一致性 |

## 5. 结论

- 全部产品调用点均已定位；M0 未改变任何公开签名，私有 `_save_active_messages_unlocked` 签名保持稳定。
- 写入路径在 M0 增加了“预检先于写入”的 fail-closed 行为，`append_message` 与 `save_active_messages` 已覆盖。
- M1/M2/M6a 接线后必须回填本清单的“目标签名/适配”与“保留字段”列，签名变更不得留下静默丢字段或跳过 CAS 的旧调用者。


## 6. M2 接口变更回填（2026-10-14）

M2 未新增产品调用点，但扩展了持久化与请求组装签名，必须同步：

| 变更 | 位置 | 兼容性 | 回归测试 | 阶段 |
| --- | --- | --- | --- | --- |
| `save_active_messages(..., expected_revision=None)` | `src/persistence/checkpoint.py` | 新增关键字参数，默认 `None` 不改变既有行为 | `test_revision_cas_rejects_concurrent_compaction` | M2-B |
| `save_active_context(..., expected_revision=None)` | `src/persistence/checkpoint.py` | 同上 | `test_revision_cas_rejects_concurrent_pin_update` | M2-B |
| `_save_active_messages_unlocked(..., *, expected_revision=None)` | `src/persistence/checkpoint.py` | 私有签名新增关键字参数；崩溃注入替身用 `*_args/**kwargs` 仍可用 | `test_session.py` 崩溃恢复组、`test_task_anchor.py` 中断恢复 | M2 |
| `Agent.run(..., pins=None, anchor=None)` | `src/core/agent.py` | 新增关键字参数，默认不注入 | `test_pinned_context.py`、`test_task_anchor.py` | M2 |
| `core/task_anchor.build_anchor(AnchorInputs)` | `src/core/task_anchor.py` | 新纯函数，无副作用 | `tests/test_task_anchor.py` | M2 |

M6a 接线时必须：REPL 传入 `pins`/`anchor`（`anchor` 由 `build_anchor` 从运行中数据构建）。
CAS 启用范围必须区分两种写入，否则会误伤：

- **追加（水位线前进）**：`save_active_messages(..., expected_seq=...)` 只传 `expected_seq`，
  **不得**传 `expected_revision`（会因“必须推进 snapshot_revision”而显式失败，见 `test_expected_revision_is_scoped_to_in_place_updates`）。
- **原地更新（水位线不变：改 pins/锚/压缩）**：`save_active_context(..., expected_revision=...)`
  或 `save_active_messages(..., expected_revision=...)`，且新上下文必须经 `with_pins`/`with_anchor`/`compact`
  产生真实变更以推进修订号；原样回写会被拒绝。

## 7. M6a 接口变更回填（2026-10-14）

| 变更 | 位置 | 兼容性 | 回归测试 | 阶段 |
| --- | --- | --- | --- | --- |
| `ActiveContext.goal`（新增可选字段） | `src/core/active_context.py` | v3 可选字段；缺失按 `""` 升级，空值不写入载荷（与 M0/M2 字节等价） | `tests/test_active_context.py`、`tests/test_pin_commands.py` | M6a |
| `ActiveContext.with_goal()` | 同上 | 新方法，原地更新并推进修订号 | `test_goal_set_view_reset_and_persist` | M6a |
| `CodingRepl.load_context()` / `commit_context()` | `src/interfaces/repl.py` | REPL 新公开方法；命令层经此读写，CAS 冲突/存储失败显式上报 | `test_cas_conflict_reports_failure_not_success` | M6a |
| `CodingRepl.refresh_anchor()` | 同上 | 锚生产者接线：由 goal/pins/WriteJournal 构建并 CAS 持久化 | `test_run_turn_injects_pins_and_anchor_without_persisting` | M6a |
| `/pin`、`/goal` 命令 | `src/commands/pin.py`、`src/commands/__init__.py` | 新命令，注册进命令表；拒绝路径形式加载 | `tests/test_pin_commands.py` | M6a |
| `ANCHOR_TASK/PINS/REASON_BUDGET`（原私有常量公开） | `src/core/task_anchor.py` | 仅重命名公开；行为不变 | `test_anchor_is_bounded_and_never_silently_drops_constraints` | M6a |

M6b 接线时必须：工厂接入全部实际入口后，把 `/pin`、`/goal`、`/context` 与 `commit_context` 参数化为
双后端；`/context` 补充实际后端信息；复用上述 CAS/Trace 路径，不得绕过 `expected_revision`。
