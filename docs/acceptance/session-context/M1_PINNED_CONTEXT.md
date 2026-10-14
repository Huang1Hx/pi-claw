# M1 验收：有界 pinned context 与压缩

- 阶段：M1（有界 pinned context 与压缩）
- 日期：2026-10-14
- 前置：M0 ACCEPTED
- 主计划：`newplan.md` §3、§4、§8(M1)
- 结论：**ACCEPTED**

## 1. 目标与非目标

**目标**

- 让 pins 与本回合 Skill 指导成为**请求前临时物化**的受限 system 块：只在发给模型的请求中出现，
  绝不写回 `messages`，因此不会被压缩摘要、不会持久化、也不会在下一回合残留。
- 有界预算：临时块与活动窗口合计超过 `max_chars` 时**明确失败**，不静默截断已接受约束。
- 保持 system 安全规则与工具调用协议不被临时块破坏。

**非目标**

- 不构建任务锚（M2）、不做 `/pin` 命令与 REPL 接线（M6a）、不做 SQLite（M3+）。
- 不新增模型调用；不改变 Skill 的选择/权限语义。

## 2. 设计要点

- `core/context.py` 新增 `EPHEMERAL_KEY` 标记与 `assemble_request(messages, ephemeral=...)`：
  在首个 `system` 之后插入带标记的临时 system 块，返回新列表，**不修改入参**。
- `ContextManager.build_request(...)` = `assemble_request` + 预算校验（`estimate_chars > max_chars` 抛错）。
- `compact()` 与 `prune_tool_results()` 防御性丢弃带 `EPHEMERAL_KEY` 的消息；临时块不进摘要。
- `Agent.run(..., pins=...)`：`pins` 与 `skill_guidance` 合并为临时块，每次模型调用前组装请求；
  不再把指导 append 进 `history`。`_strip_guidance` 退化为按标记清除的防御性兜底。
- 单一权威数据：pins 只存在于结构化状态（M0 的 `ActiveContext.pins`），`messages` 里不存合成副本。

## 3. 验收项

| 验收项 ID | 场景与预期 | 测试/命令 | 实际结果 | 证据 | 结论 |
| --- | --- | --- | --- | --- | --- |
| M1-1 | 请求含 pins/指导，`messages` 不含且角色序不变 | `test_pins_materialized_in_request_but_not_persisted` | passed | `tests/test_pinned_context.py` | PASS |
| M1-2 | 连续 20 次压缩 pins 保留率 100%，且从未进入普通消息 | `test_pins_survive_twenty_compactions` | passed | 同上 | PASS |
| M1-3 | Skill 指导在压缩后无原文/摘要残留 | `test_guidance_leaves_no_residue_after_compaction`、`test_compaction_drops_premarked_ephemeral_messages` | passed | 同上 | PASS |
| M1-4 | 临时块与窗口合计超预算明确失败 | `test_over_budget_request_fails_explicitly` | passed | 同上 | PASS |
| M1-5 | 工具正文不能成为 pin/授权，只能作为低信任数据 | `test_tool_output_cannot_become_pin` | passed | 同上 | PASS |
| M1-6 | 插入 pins 不破坏 tool 调用配对 | `test_request_with_pins_preserves_tool_protocol` | passed | 同上 | PASS |
| M1-7 | 既有 Skill 单回合注入/剥离语义兼容 | `tests/test_skills_agent_injection.py` | 3 passed | 同上 | PASS |
| M1-8 | 全量回归与静态检查 | `pytest -o addopts="" -q`、`ruff check`、`ruff format --check` | `4 failed(既有), 933 passed, 3 skipped`；Ruff/格式通过 | 命令输出 | PASS |

## 4. 裁决记录（Ruling）

- Ruling：临时块用**标记 + 每次请求重组**，而非“append 进 history 再按对象身份剥离”。
  理由：`compact()` 会深拷贝并重建列表，对象身份剥离在压缩后失效，指导文本会经摘要泄漏；
  改为请求前物化后，`messages` 从源头就不含临时块。代价：多一次浅拷贝请求列表，可忽略。
- Ruling：超预算直接抛错而非丢弃 pins。理由：已接受的硬约束不能静默截断；与计划 §4 一致。

## 5. 未完成项与下一阶段准入

- 任务锚（`anchor`）尚未物化，属 M2；M1 只做 pins 与 Skill 指导。
- `/pin`、`/context` 展示与 Trace 事件、REPL 接线属 M6a。
- 准入 M2：M1 全部必选项 PASS，无阻塞；`assemble_request`/`build_request` 已成为请求组装的唯一入口。
