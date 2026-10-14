# M0 验收：基线与共享契约

- 阶段：M0（基线与共享契约）
- 日期：2026-10-14
- 代码基线：`optimize` @ `993881f`（本阶段改动以此为基础，尚未提交）
- 主计划：`newplan.md` §3、§5、§6、§8(M0)
- 结论：**ACCEPTED**（M0-A / M0-B / M0-C 全部满足；证据见 §2、§3.6、`M0_CALL_SITES.md`）

## 1. 目标与非目标

**目标**

- 复核 Skill/上下文/会话基线，用可复现证据澄清历史失败。
- 固定 `ActiveContext` 与 `SessionRepository` 的字段、接口签名、序号与修订号职责、预算和错误语义。
- 先写旧格式/空锚/旧接口/日志尾恢复的失败测试，再实现兼容契约。
- 产出调用点清单，约束后续 M1/M2/M6 的改动边界。

**非目标**

- 不实现 pinned context 的压缩保活（M1）、任务锚构建（M2）、SQLite 后端（M3）与迁移（M5）。
- 不接线 CLI/REPL 命令（M6），不做评测（M7）。
- 不改评分器、不改历史冻结结果、不引入外部服务或向量库。

## 2. 本地基线证据（复核历史失败）

环境：macOS-27.0-arm64、Python 3.12.13、pytest 9.1.1，工作树在 M0 首次运行时为干净状态（`optimize` @ `993881f`）。
命令：`.venv/bin/python -m pytest -o addopts="" -q`

| 状态 | 结果 |
| --- | --- |
| 改动前基线 | `4 failed, 894 passed, 3 skipped` |
| 改动后 | `4 failed, 917 passed, 3 skipped` |

改动前后**失败集合完全一致**，均为既有环境相关失败，与本阶段契约改动无关：

- `tests/test_sandbox_lifecycle.py::test_host_runner_kills_background_children_in_same_process_group`
- `tests/test_sandbox_lifecycle.py::test_host_runner_cleans_background_child_after_parent_exits_normally`
- `tests/test_terminal_experience.py::test_cli_saves_actual_code_inside_selected_workspace[False]`
- `tests/test_terminal_experience.py::test_cli_saves_actual_code_inside_selected_workspace[True]`

`test_terminal_experience` 的失败原因是本地无网络时工作区内 `uv run python -m unittest` 退出码为 2，
属环境依赖；按 `ITERATION_041` 记录这 4 项在 CI 侧为通过。本阶段未改动这些路径，失败集合不变即为
“与本轮契约改动无关”的直接证据。新增测试 31 项（`test_active_context.py` 24、`test_session_repository_contract.py` 7）全部通过。

“既有失败”的认定依据：M0 首次全量运行发生在**未做任何 M0 改动、工作树干净**的 `993881f` 上，失败集合与本阶段改动后完全一致；
这 4 项测试不涉及上下文/持久化/会话契约路径。

### 2.1 CI 证据（与本地分开取证，不互推）

| 项目 | 值 |
| --- | --- |
| Run | `ci` run **38428830418** |
| URL | https://github.com/Huang1Hx/pi-claw/actions/runs/38428830418 |
| 提交 SHA | `993881faa49125538906794059995d463f3d2f5b`（`optimize`） |
| 触发/结论 | push / success |
| 环境 | Ubuntu（GitHub Actions，Python 3.12） |
| 通过步骤 | Set up job、checkout、setup-python、Install pinned uv、Install locked dependencies、Lint、Format check、Compile、Test、Build distributions |
| 覆盖差异 | CI 为 Ubuntu/Linux + 锁定依赖；本地为 macOS-27.0-arm64 + `.venv`。CI 通过**不**证明本地 4 项失败消失，也不替代 Docker 专项验收；本阶段未据此推断本地全绿。 |

证据来源：`gh run view 38428830418 --repo Huang1Hx/pi-claw` 与 `gh run view --job=115405308151`（2026-10-14 核对）。

## 3. 契约决策（M0 固定）

### 3.1 快照版本策略

- 写入恒为 `version: 3`；读取接受 `v2`（dict）与 `v3`（dict），`v1`（list）回退到 journal 重放。
- 未知版本（如 `4`、非整型）→ `ActiveContextError`（`ValueError` 子类），**fail closed**。
- 旧二进制读到 v3 会因 `version != 2` 报“版本不受支持”，即降级为显式失败而非静默丢字段（当前实现的既有行为，予以保留）。

### 3.2 字段语义

`ActiveContext`：`messages`、`through_seq`、`snapshot_revision`、`pins`（结构化元组）、`anchor`（可空）、`version`。

| 字段 | 语义 | 谁保护 |
| --- | --- | --- |
| `through_seq` | 活动快照已吸收的日志水位线 | `expected_seq`（=journal seq）CAS |
| `snapshot_revision` | 随写入递增的修订计数，标注水位线不变的原地编辑 | **尚未**提供并发 CAS；实现在 M2/M4（见 §3.7） |
| `anchor.covered_through_seq` | 锚构建器检查到的日志位置 | M2 使用；不作为“最后证据位置” |

### 3.3 预算（M0 固定）

- `ANCHOR_MAX_CHARS = 2000`、`PIN_MAX_CHARS = 500`、`MAX_PINS = 32`、`PINS_MAX_CHARS = 4000`。
- 全部计入上下文总预算（M1 接线）。超限时 `ActiveContextError`，不静默截断已接受约束。

### 3.4 与 v2 的体积等价

`to_payload()` **省略默认值字段**（`snapshot_revision==0`、`pins==()`、`anchor is None`），使无 pins/anchor 的
快照与 v2 载荷字节数一致，从而保持既有极小字节预算行为（既有 `max_bytes=80` 用例仍成立）。

### 3.5 修订号推进规则（Ruling）

`with_messages()` 仅在**水位线不变且消息内容变化**（如 `/compact`）时推进 `snapshot_revision`；
推进水位线的追加已由 `expected_seq` 保护，不再重复计数。理由：避免默认快照因新增 `snapshot_revision`
字段变大而破坏既有字节预算等价；代价：追加路径不产生独立修订号，但该路径本就有 seq 版本保护。

### 3.6 版本升级隔离（M0-B）

- 新写入恒为 v3；读取顺序：v1（list，回退 journal）→ v2（升级，pins/anchor 为空）→ v3。
- 未知版本、畸形 v3、非对象载荷 → `ValueError` **fail closed**，且**预检先于任何 journal/state/snapshot 写入**：
  `append_message` 在写 journal 前调用 `_precheck_snapshot_unlocked`；`save_active_messages` 在写快照前读回校验。
- 旧程序读取 v3 会因 `version != 2` 显式报“版本不受支持”，不会静默忽略 pins/anchor（测试模拟旧读取器验证）。
- 旧写路径风险：旧二进制的 `save_active_messages` 可直接覆盖快照，`append_message` 可能先写 journal，
  因此“旧读取器拒绝 v3”不等于“旧进程写不坏数据”。升级前必须停止共享目录的旧写入进程并备份，
  否则改用隔离的新数据目录；本阶段不宣称已保护旧二进制的写接口。
- 非空 pins/anchor 无法无损导出旧格式时必须拒绝，不以“回滚”名义丢字段。

### 3.7 修订号 CAS 尚未启用（M0 明确契约）

`snapshot_revision` 目前只是随写入递增的计数，用于标注“水位线不变的原地编辑”（如 `/compact`、改 pins）。
它**尚未**作为并发比较依据：
- `expected_seq` 只反映 journal 日志序号，**无法识别不追加消息的并发更新**。两个实例读取同一状态后各自
  追加 pin 并保存，当前两次都会成功，后写覆盖先写（丢失更新）。
- M0 **不声称**已具备该保护；接口契约（`save_active_context` 的 docstring）已明确这一点。
  基于 `snapshot_revision` 的比较-写入（CAS）实现归入 M2/M4，届时需新增并发回归测试。

## 4. 调用点清单

产品代码中与活动快照/日志相关的调用点（M1/M2/M6 改动必须覆盖）：

| 调用点 | 方法 | 备注 |
| --- | --- | --- |
| `src/interfaces/repl.py:121` | `load_active_snapshot` | 会话恢复 |
| `src/interfaces/repl.py:126` | `save_active_messages` | 启动时回写 |
| `src/interfaces/repl.py:222` | `append_message` | 用户回合追加 |
| `src/interfaces/repl.py:325` | `record_usage` | 用量幂等 |
| `src/interfaces/repl.py:329` | `save_active_messages` | 回合结束保存 |
| `src/commands/sessions.py:9,14` | `load_active_snapshot` / `save_active_messages` | `/new` |
| `src/commands/sessions.py:73,78` | `load_active_snapshot` / `save_active_messages` | `/resume` |
| `src/commands/context.py:8` | `save_active_messages` | `/compact` |
| `src/persistence/session_store.py:77,82` | `append_message` / `save_active_messages` | `create` |
| `src/persistence/journal.py:73,76-77` | 内部快照保存/读取 | 追加路径保留 pins/anchor |

新接口 `load_active_context` / `save_active_context` 当前无产品调用点，供 M1/M2 使用。
`load_active_messages` 无产品调用点（仅测试与外部兼容）。

## 5. 验收项

| 验收项 ID | 场景与预期 | 测试/命令 | 实际结果 | 证据 | 结论 |
| --- | --- | --- | --- | --- | --- |
| M0-1 | 历史失败可复现且与本轮无关 | `pytest -o addopts="" -q` | 4 failed 集合前后一致 | 本文件 §2 | PASS |
| M0-2 | v3 往返、v2 升级、未知版本拒绝 | `tests/test_active_context.py` | 16 passed | 同上 | PASS |
| M0-3 | 旧接口保存消息不擦除 pins/anchor | `test_store_message_save_does_not_erase_pins_or_anchor` | passed | 同上 | PASS |
| M0-4 | 无 pins/anchor 载荷与 v2 体积等价 | `test_default_payload_omits_optional_fields`、`test_active_message_write_limits` | passed | 同上 | PASS |
| M0-5 | 日志尾恢复、v1 迁移、seq 冲突 | `tests/test_session.py` | 全通过 | 同上 | PASS |
| M0-6 | `SessionRepository` 端口存在且 files 实现满足 | `tests/test_session_repository_contract.py` | 7 passed | 同上 | PASS |
| M0-7 | 静态检查与格式 | `.venv/bin/python -m ruff check src tests`、`ruff format --check src tests` | All checks passed / 172 formatted | 命令输出 | PASS |
| M0-8 | 旧读取器拒绝 v3（降级为显式失败） | `test_v3_snapshot_is_rejected_by_v2_only_reader` | passed | §3.6 | PASS |
| M0-9 | 未知版本预检先于写入，失败后数据不变 | `test_unknown_snapshot_version_fails_before_any_write` | passed | §3.6 | PASS |
| M0-10 | 调用点清单完整且有测试映射 | `M0_CALL_SITES.md` | 覆盖产品调用点/包装器/测试替身 | 该文档 | PASS |
| M0-11 | 中断→恢复→保存→再恢复不重复消息，水位线同步 | `test_interrupted_recovery_then_save_does_not_duplicate_messages` | passed | §5b 复审 | PASS |
| M0-12 | `save_active_context` 写入前预检既有版本 | `test_save_active_context_prechecks_existing_version` | passed | §5b 复审 | PASS |
| M0-13 | messages/through_seq 为必填，缺失拒绝 | `test_missing_required_fields_are_rejected` | passed | §5b 复审 | PASS |
| M0-14 | 保存完整上下文前拒绝超预算/超前水位线 | `test_save_active_context_rejects_oversized_and_ahead_of_journal` | passed | §5b 复审 | PASS |
| M0-15 | 修订号 CAS 契约明确且未过度声明 | 本文件 §3.7、`repository.py` docstring | 已修正 | §3.7 | PASS |

## 5b. M0 复审修复（2026-10-14）

对 `ec991bb..46b49dd` 的复审发现 5 项问题，均已处理：

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | 中断恢复后再次保存重复消息（水位线未同步） | `_load_active_context_unlocked` 返回的水位线同步到实际吸收位置；补 M0-11 回归 |
| 2 | `save_active_context` 绕过未知版本保护 | 锁内写入前调用 `_precheck_snapshot_unlocked`；补 M0-12 |
| 3 | 缺失必填字段的快照被当合法 | `from_payload` 要求 `messages`/`through_seq` 必填；补 M0-13 |
| 4 | 能保存本仓库读不回的快照 | 保存前校验 `max_events` 与 `through_seq <= journal`；补 M0-14 |
| 5 | 修订号无 CAS 却被暗示有保护 | 明确契约缺口（§3.7），修正字段说明与协议 docstring；补 M0-15 |

修复后本地全量：`4 failed（既有）, 925 passed, 3 skipped`。

## 6. 裁决记录（Ruling）

- Ruling：`to_payload()` 省略默认值字段 — 保持与 v2 字节等价，避免破坏既有预算用例 — 若判断错误，
  影响仅是多写入若干字节的冗余字段，可无损回退。
- Ruling：`snapshot_revision` 仅在水位线不变的原地编辑时推进 — 避免与 `expected_seq` 重复建模，
  并保持默认快照体积 — 若判断错误，代价是原地编辑之外的修订号粒度不足，M4 可再补。

## 7. 未完成项与下一阶段准入

- 未接线：`pins/anchor` 尚未进入请求组装、压缩保活与命令显示，属 M1/M2/M6 范围。
- SQLite 后端尚未实现，契约测试当前只参数化 `files`，M3 扩展 `BACKENDS` 即可。
- 准入 M1：M0 全部必选项 PASS，无阻塞；接口签名、预算、序号职责已固定。
