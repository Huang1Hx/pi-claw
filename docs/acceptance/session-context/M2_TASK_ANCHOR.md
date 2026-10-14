# M2 验收：TaskAnchor 与文件后端恢复

- 阶段：M2（任务锚与文件后端恢复）
- 日期：2026-10-14
- 前置：M1 ACCEPTED
- 主计划：`newplan.md` §3、§4、§5、§8(M2)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 确定性、有界、零模型调用的任务锚构建：仅从已在手数据（当前目标、用户 pin、成功写入记录、验证结果）生成。
- 目标切换 / pin 撤销 / 写入记录不完整 / 验证失效等生命周期显式反映在锚文本与哈希中。
- 锚接入文件快照并可跨进程重启恢复；恢复后模型实际看到的输入包含锚。
- 文件后端修订号 CAS（M2-B）：相同 `seq` 的 pin/锚/压缩并发只允许一个提交。

**非目标**

- 不从运行中的 Agent 循环自动采集并构建锚（生产者接线属 M6a）；不做 `/pin` 命令（M6a）。
- 不做 SQLite/迁移（M3–M5）、不做综合评测（M7a/M7b）。

## 2. 设计要点

- `core/task_anchor.py`：`AnchorInputs`（结构化、无原始工具正文）+ `build_anchor()` 纯函数。
  - 目标截断有界；**约束（pins）绝不截断**，放不下显式报错；整体 ≤ `ANCHOR_MAX_CHARS`。
  - 写入记录不完整/省略数量显式标注；验证结果绑定工作区指纹，指纹变化标注“已失效”。
  - `content_hash` 只由语义内容决定（`updated_at` 不参与），相同输入 → 相同文本与哈希。
- 持久化：锚存于 `ActiveContext.anchor`，经 `save_active_context` 写入 v3 快照；读取/恢复不推进修订号。
- 请求组装：`Agent.run(active=...)` 把锚作为**临时块**（M1 的 `EPHEMERAL_KEY`）在请求前物化，绝不落 `messages`。
- 修订号 CAS：`save_active_context`/`save_active_messages` 可选 `expected_revision`；不一致显式失败，
  并要求写入推进 `snapshot_revision`，防止丢失更新。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试/命令 | 实际结果 | 证据 | 结论 |
| --- | --- | --- | --- | --- | --- |
| M2-1 | 相同输入 → 相同文本与哈希（时间字段不参与） | `test_same_inputs_produce_identical_text_and_hash`、`test_anchor_is_deterministic_across_repeated_builds` | passed | `tests/test_task_anchor.py` | PASS |
| M2-2 | 目标切换替换旧目标 | `test_task_switch_replaces_old_goal` | passed | 同上 | PASS |
| M2-3 | pin 撤销后不再出现在锚中 | `test_revoked_pin_disappears_from_anchor` | passed | 同上 | PASS |
| M2-4 | 写入记录不完整/省略数量显式标注 | `test_incomplete_write_journal_is_marked_explicitly` | passed | 同上 | PASS |
| M2-5 | 新写入使旧验证失效 | `test_verification_is_invalidated_after_new_writes` | passed | 同上 | PASS |
| M2-6 | 锚有界，约束不被静默截断 | `test_anchor_is_bounded_and_never_silently_drops_constraints` | passed | 同上 | PASS |
| M2-7 | 锚跨重启恢复并在请求中出现，且不落 messages | `test_anchor_survives_restart_and_appears_in_request` | passed | `tests/test_pinned_context.py` | PASS |
| M2-8 | 中断追加后恢复：锚仍在、journal 尾部不丢 | `test_anchor_survives_interrupted_append_then_recovery` | passed | `tests/test_task_anchor.py` | PASS |
| M2-9 | 兼容包装器不擦除锚且推进修订号 | `test_legacy_message_api_preserves_anchor_and_bumps_revision` | passed | 同上 | PASS |
| M2-10 | M2-B 相同 seq 的 pin/压缩并发只有一个提交 | `test_revision_cas_rejects_concurrent_pin_update`、`test_revision_cas_rejects_concurrent_compaction` | passed | 同上 | PASS |
| M2-11 | 锚构建无网络/子进程外部 IO，重复构建一致 | `test_build_anchor_is_pure_without_external_io` | passed | 同上 | PASS |
| M2-12 | 全量回归与静态检查 | `pytest -o addopts="" -q`、`ruff check`、`ruff format --check` | 见 §5 | 命令输出 | PASS |
| M2-13 | CAS 作用域：原地更新用 `expected_revision`；追加路径携带旧修订号必须显式失败，仅用 `expected_seq` 可提交 | `test_expected_revision_is_scoped_to_in_place_updates` | passed | 同上 | PASS |

## 4. 裁决记录（Ruling）

- Ruling：M2 只交付**锚构建器 + 持久化/组装契约 + CAS**，运行中数据采集与 REPL 接线放 M6a。
  理由：M2 的验收聚焦“锚本身正确、可恢复、并发安全”，命令层属 M6a；避免在一阶段塞入两层改动。
- Ruling：订正号 CAS 采用**可选 `expected_revision`**。理由：保持既有调用点（REPL/命令）默认行为不变，
  由 M6a 显式接线启用；未传时不做 CAS，`repository.py` 与本文档均已如实标注该边界。
- Ruling：锚文本中 `pins` 按传入顺序拼接，未排序。理由：pin 的加入顺序可能承载语义；
  相同输入顺序下确定性成立。若后续需要跨顺序稳定，可在 M6a 统一排序。

## 5. 证据与结论

- 专项：`tests/test_task_anchor.py` 13 项、`tests/test_pinned_context.py` 10 项全部通过。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 949 passed, 3 skipped`。
- Ruff/格式：`ruff check src tests`、`ruff format --check src tests` 通过。

**结论：ACCEPTED**（M2-A / M2-B 满足；生产者接线与命令层留待 M6a，已在 §1/§4 明确）。


## 6. 自审记录（两次）

**自审 #1（`dbf57e1..a96e88f`，核心实现）**

- 结论：无高危缺陷。CAS 为**可选生效**（需传 `expected_revision`）；锚的**生产方**（从运行中数据采集）
  尚未接线；`pins` 按传入顺序拼接（相同顺序确定）。以上均不阻断 M2 库级验收，已记入 §1/§4。
- 复核点：修订号比较在锁内、写入前完成；`_save_active_messages_unlocked` 签名新增关键字参数后，
  历史崩溃注入替身（`*_args/**kwargs`）仍可用。

**自审 #2（M2 收口 diff，含测试与文档）**

- 结论：通过。逐条复核：
  - 中断恢复用例：快照（含锚）先落盘 → 追加时 `_save_active_messages_unlocked` 抛错 → journal 尾部保留 →
    恢复后锚仍在、消息不丢；与 `_load_active_context_unlocked` 的水位线同步逻辑一致。
  - 兼容包装器：`save_active_messages` 读回既有锚并保留，原地压缩推进修订号，未擦除新字段。
  - CAS 竞争用例：两个实例同修订号各自加 pin/压缩，只有一个提交，另一个显式失败。
  - 文档一致性：`M0_CALL_SITES.md` 已回填 M2 的签名变更与 M6a 接线要求；本文档未声称已具备
    未实现的保护（CAS 明确为可选）。
- 遗留（非缺陷）：重复读取既有快照两次（预检 + CAS 判定），可在后续合并为一次读；不影响正确性。

**自审 #3（`a96e88f` 复评，M6a 前置）**

- 结论：核心逻辑无高危缺陷；修正两处“表述与实现不完全对齐”的问题，不改变运行行为。
- 发现 1（已修，测试质量）：`test_build_anchor_is_pure_without_model_calls` 原断言只检查序列化串含
  `t`/`p`，无法证明“零模型/零外部依赖”，属诚实标注缺口。已重写为 `..._without_external_io`：
  monkeypatch `socket.socket`/`create_connection`/`subprocess.Popen`/`run` 抛错后构建仍成功，并校验重复构建哈希一致。
- 发现 2（已记录，接口边界）：`expected_revision` 只能用于**水位线不变**的原地更新。实测在追加后携带旧
  修订号保存会抛 `ValueError("...必须推进 snapshot_revision...")`。这是**有意**的：若允许，两个写者会在同一
  修订号上先后覆盖，CAS 失效。追加路径由 `expected_seq` 提供互斥，应只传 `expected_seq`。已新增
  `test_expected_revision_is_scoped_to_in_place_updates` 固化该契约，供 M6a 接线遵循。
- 发现 3（非缺陷，已确认）：`save_active_context` 在传入 `expected_revision` 时要求写入推进修订号，
  因此“读取后原样回写”会被拒绝；命令层必须先经 `with_pins`/`with_anchor` 产生真实变更再保存。
