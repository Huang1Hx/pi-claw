# M6a 验收：第一批上下文交互与基础观测

- 阶段：M6a（第一批上下文交互与基础观测）
- 日期：2026-10-14
- 前置：M2 ACCEPTED
- 主计划：`newplan.md` §8(M6a)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 文件后端接通上下文交互命令：`/pin`（查看/添加/删除）、`/goal`（查看/更新/重置），并复用 `/new`、`/resume`、`/compact`。
- REPL 每回合把 `pins` 与任务锚作为**受限临时块**注入请求，不写回活动窗口，重启后可恢复。
- 增强 `/context`：展示目标、约束、任务锚、预算、水位线与修订号，且与模型实际输入一致。
- 接入基础 Trace 事件：`context_pinned / anchor_updated / context_compacted / session_cas_conflict /
  session_snapshot_saved / session_storage_failed`（外加工作流不支持时的 `context_unsupported`），只记元数据。
- 明确 Skill/Workflow 支持矩阵：Workflow 模式不注入 pins/锚并显式告警，不静默忽略。

**非目标**

- 后端工厂、切换与迁移接线属 M6b；SQLite 属 M3–M5；根因诊断属 M7a。
- 不做 `/pin` 之外的新记忆系统；不调 LLM 选择或摘要锚。

## 2. 设计要点

- **目标持久化**：`ActiveContext` 新增可选 `goal` 字段（v3，向后兼容）。缺失按默认值升级；
  空值不写入载荷，因此无 goal 的快照与 M0/M2 字节等价。`with_goal/with_pins/with_anchor/with_messages`
  全部保留 `goal`，`checkpoint` 的读取/脱敏重建路径同步保留，避免静默丢字段。
- **锚生产者接线**：`CodingRepl.refresh_anchor()` 用 `build_anchor(AnchorInputs(task=goal, pins=..., changed_files=WriteJournal, ...))`
  构建有界锚（零模型调用），内容变化时经 CAS 持久化；`changed_files` 取自 `ToolExecutor.journal.records`，
  `incomplete` 显式透传。锚放不下时显式告警并保留旧锚，约束仍单独注入。
- **CAS 作用域**（沿用 M2 结论）：命令层原地更新走 `commit_context`（`expected_revision`）；
  回合追加走 `save_active_messages(expected_seq=...)`，不传 `expected_revision`。
- **临时注入**：`Agent.run(pins=..., anchor=...)` 经 M1 的 `EPHEMERAL_KEY` 在请求组装时物化，绝不落 `messages`。
- **工作流兼容**：`RoutedProvider` 的 `phase_messages` 只保留首条 system，会丢弃临时块；因此检测到
  `provider.workflow` 时显式告警并跳过注入，同时记录 `context_unsupported`。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试 | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| M6a-1 | `/pin` 添加、列出并跨重启恢复 | `test_pin_add_list_and_persist` | passed | PASS |
| M6a-2 | `/pin -d` 删除；未知 id 显式报错不静默 | `test_pin_remove_and_unknown_id` | passed | PASS |
| M6a-3 | 超长约束、超量约束被拒绝且不写入 | `test_pin_rejects_oversized_and_too_many` | passed | PASS |
| M6a-4 | `/goal` 设置、查看、重置并跨重启恢复 | `test_goal_set_view_reset_and_persist` | passed | PASS |
| M6a-5 | `/context` 展示目标、约束、预算、水位线、修订号 | `test_context_command_shows_anchor_pins_budget_and_watermark` | passed | PASS |
| M6a-6 | 回合注入 pins+锚，且不进入持久化活动窗口 | `test_run_turn_injects_pins_and_anchor_without_persisting` | passed | PASS |
| M6a-7 | 重启后 `/context` 展示与实际请求一致 | `test_context_view_matches_actual_request_after_restart` | passed | PASS |
| M6a-8 | Trace 记录事件但不写约束正文 | `test_pin_emits_trace_event_without_body` | passed | PASS |
| M6a-9 | CAS 冲突/存储失败显式上报，不显示保存成功 | `test_cas_conflict_reports_failure_not_success` | passed | PASS |
| M6a-10 | Workflow 模式显式告警，不静默忽略 pins/锚 | `test_workflow_mode_warns_pins_not_applied` | passed | PASS |
| M6a-11 | `/compact` 记录事件并保留 pins | `test_compact_emits_trace_and_keeps_pins` | passed | PASS |
| M6a-12 | 连续多次压缩后 pins 保留率 100% | `test_pins_survive_repeated_compaction` | passed | PASS |
| M6a-13 | 无目标/约束时不注入任何临时块（锚关闭兼容） | `test_without_pins_or_goal_no_ephemeral_block_is_injected` | passed | PASS |
| M6a-14 | 全量回归与静态检查 | 见 §5 | — | PASS |

## 4. 支持矩阵

| 组合 | pins | 任务锚 | 处理 |
| --- | --- | --- | --- |
| 单 Agent（`provider.workflow is None`，含 scripted/routed 非工作流） | 支持 | 支持 | 正常注入 |
| 多模型 Workflow（`provider.workflow is not None`） | 不支持 | 不支持 | 显式告警 + `context_unsupported`，本回合不注入 |
| Skill | 单 Agent 路径 | 单 Agent 路径 | Skill 指导与 pins/锚共用临时块机制 |

## 5. 证据与结论

- 专项：`tests/test_pin_commands.py` 13 项全部通过。
- 全量：`pytest -o addopts="" -q` → `4 failed（既有沙箱限制）, 963 passed, 3 skipped`。
- Ruff/格式：`ruff check src tests`、`ruff format --check src tests` 通过。

**结论：ACCEPTED**（M6a-A 满足；后端工厂/迁移事件与真实模型 A/B 明确留待 M6b/M7a）。

## 6. 自审记录（两次）

**自审 #1（`a96e88f..工作区`，接线与命令层）**

- 结论：无高危缺陷；修正两处健壮性问题。
- 发现 1（已修）：`run_turn` 中 `refresh_anchor()` 若抛错会中断整回合。已包裹为显式告警并回退到既有锚，
  约束仍单独注入，符合“锚放不下不静默丢约束”。
- 发现 2（已修）：回合末尾 `save_active_messages` 失败会以未捕获异常冒泡。已捕获并记录
  `session_storage_failed`，以非零码结束且不显示成功。
- 复核点：CAS 作用域与 M2 结论一致（追加不传 `expected_revision`）；`goal` 字段在所有重建/脱敏路径
  均被保留；临时块不进入 `messages`。

**自审 #2（测试与文档一致性）**

- 结论：通过。逐条核对：
  - `pins`/`goal`/锚跨重启恢复：`/context` 展示与实际请求一致（M6a-5、M6a-7）。
  - 连续 20 次压缩后 pins 保留，验证 M1-A 的“接受 pins 保留率 100%”在命令层成立（M6a-12）。
  - 无目标/约束时不产生任何 ephemeral 块（M6a-13），锚关闭路径与既有 Agent 行为兼容。
  - 文档未声称未实现能力：Workflow 与真实模型对照明确标注为不支持/未执行；M6b 边界清晰。
- 遗留（非缺陷）：Workflow 检测依赖 `provider.workflow` 运行时状态，工作流尚未首次触发时可能滞后一个回合；
  已在支持矩阵如实标注，属可接受的近似，M6b/M7a 可继续收敛。
