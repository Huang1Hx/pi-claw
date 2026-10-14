# Pi-Claw 会话上下文一致性与可选长期记忆实施计划

> **执行说明：** 接续已有 Skill MVP，按第 7 节的两批交付顺序实施；每阶段先定义验收标准、补失败测试，
> 再实现、验证、形成验收文档和审查结论。只有标为 `ACCEPTED` 才能进入下一阶段。
> 采用当前会话内逐项执行方式，实施时使用 `superpowers:executing-plans`；本次计划落盘不代表开始实施。

**目标：** 提高长任务执行、会话恢复和并发持久化的正确性，并以可复核证据决定是否扩展跨会话知识复用。

**架构：** 任务锚和 pins 负责当前任务的上下文，SessionRepository 负责同一份活动快照的保存与恢复；
Skill 继续提供单回合执行指导。可选长期记忆使用独立的数据模型和仓储，不取代会话、Checkpoint 或 Trace。

**技术栈：** Python、uv、现有 ContextManager / SessionStore / Skill / Policy / Approval / Sandbox、
标准库 sqlite3、pytest、Ruff；默认不增加外部服务或模型调用。

**设计依据：** 本文第 2–6 节保留已讨论的融合设计，第 7–10 节为执行与验收要求；
现有 Skill 以 [SKILLS.md](docs/SKILLS.md) 和实际代码为准，原 P0–P6 计划保留在文末历史附录。

## 1. 当前状态与执行入口

- 编制基线为 `edfceef`；融合计划首次提交为 `993881f`。这些是固定的历史参照，不表示分支当前指向；
  每次执行先核对实际 Git 状态，避免计划的下一次提交使“当前提交”描述再次过时。
- Skill P0–P6 已有实现和提交，不重复开发。Skill 默认关闭、本地确定性选择、单回合受限指导，
  当前设计仅在单 Agent 路径激活；需要核实实际调用链，不能假定多模型 Workflow 已支持。
- [Iteration 041](docs/iterations/ITERATION_041_SKILLS_MVP.md) 仍有发布状态陈旧、历史全量测试
  4 项失败以及真实模型 A/B 待验证的记录。本次只更新计划，未重新执行这些测试，M0 必须复核。
- 2026-10-14 已核实：`993881f` 的 [常规 CI 运行 38428830418](https://github.com/Huang1Hx/pi-claw/actions/runs/38428830418)
  成功，环境为 Ubuntu 24.04 / Python 3.12，Lint、Format、Compile、Test、Build 步骤通过。
  该证据只覆盖这次运行的配置，不证明本地历史 4 项失败已消失或属于既有缺陷，也不替代专门的 Docker 验收。
- M0、M1、M2、M6a 已 ACCEPTED；下一步 M7a（第一批专项验收与可展示交付）；E 阶段为条件扩展。
- 先阅读适用的 AGENTS.md、Git 状态、最近提交及现有计划；保留用户已有修改，不覆盖或丢弃。
- 优先使用 `optimize`。分支不同或有重叠修改时先检查原因，不能强行切换或覆盖。

执行前重点阅读 `src/skills/`、`src/core/context.py`、`src/core/agent.py`、`src/core/journal.py`、
`src/persistence/`、`src/interfaces/{cli,repl}.py`、`src/commands/`、相关安全实现与测试。

## 2. 范围、职责和约束

本轮必做：任务锚、pinned context、统一活动快照、SessionRepository、兼容文件后端、SQLite 后端、
并发与崩溃验证、双向迁移、终端接线、Trace、量化评测及验收文档。

条件扩展：显式跨会话知识、关键词检索、Skill 按需检索；Embedding 和 Redis 最后按证据决定。

**交付范围已确定：** 保留 M0 独立验收。第一批先交付 M0–M2，以及前移的 M6a 最小交互/Trace 和
M7a 上下文专项验收，形成仅用文件后端、无新增运行依赖的任务锚可用版本；第二批完成 SQLite、
迁移、后端切换和综合评测。不能只完成 M2 就称为可发布，也不需要等待 SQLite 才展示任务锚。

| 组件 | 唯一职责 | 明确边界 |
| --- | --- | --- |
| Skill | 任务执行方法 | 不扩大权限，不复制 Workflow，不永久保存单回合指导 |
| TaskAnchor / pins | 当前目标、约束和经过验证的进展 | 会话内，不自动成为长期记忆 |
| ContextManager | 模型输入组装、预算与压缩 | 不能静默破坏硬约束和工具协议 |
| SessionRepository | 会话事件、快照、元数据、Usage | 不承担跨会话语义检索 |
| MemoryRepository（可选） | 经确认的跨会话知识 | 不复制原始对话、Trace 或 Checkpoint |

- 复用 Journal、Checkpoint、TraceWriter、Usage、Policy、Approval、Sandbox、CompletionVerifier。
- 默认路径不增加模型调用；锚增加的上下文和 Token 必须计入，不能宣称零 Token 开销。
- 不放宽权限、不改评分器、不覆盖历史冻结结果、不通过隐藏失败或未约定修复轮次提高成绩。
- 不提交凭据、私有 Base URL、原始 Session/Trace 或未脱敏产物；面试、简历材料保持在仓库外。
- 使用 uv，关键接口、并发和安全边界补中文注释；不进行无关目录重构。

## 3. 共享数据契约和审查重点

### ActiveContext 与请求组装

值对象至少包含 `version`、普通活动 `messages`、`through_seq`、`snapshot_revision`、结构化 `pins`
和可为空的结构化 `anchor`。**新写入固定采用 `version:3`，禁止继续写 v2 再附加新字段。**

### 快照版本、升级和降级

| 路径 | 必须行为 |
| --- | --- |
| 新程序读取 v1/v2 | 兼容读取，缺失 pins/anchor 恢复为空；继承 v1 以耐久 journal 为准及 v2 补齐尾部的语义 |
| 新程序保存旧格式或新会话 | 在版本/修订校验及锁保护下写完整 v3；单纯读取不自动改写磁盘 |
| 新程序读取/更新未知格式或畸形 v3 | 明确失败；版本/格式预检在任何相关日志、状态或快照修改之前完成 |
| 旧程序正常读取 v3 | 明确报不支持；不得靠沿用 v2 让旧程序静默忽略 pins/anchor |
| 用户回退旧程序 | 停止写入后恢复兼容备份，或显式导出到另一目录；不能原地丢字段降级 |

旧二进制的写路径并非全部检查现有快照：`save_active_messages()` 可直接覆盖，`append_message()`
可能先写 journal 再检查快照。因此，“旧读取器拒绝 v3”不等于“旧进程无法写坏数据”。升级前必须停止
共享目录的旧写入进程并备份；无法确认停止时拒绝原地升级，改用明确隔离的新数据目录，不在其间混写。
不能声称新代码能约束仍持有旧实现的进程。

M0 固定并测试：v1/v2→v3、未知版本拒绝、旧版本正常恢复 v3 失败、失败前后数据状态，以及旧写接口
误用的实际风险与升级隔离约束。非空 pins/anchor 无法无损导出旧格式时必须拒绝，不能以“回滚”名义丢弃。
具体兼容默认值和初始 `snapshot_revision` 在 M0 固定，兼容包装器不得绕过后续快照 CAS。

### 请求组装与序号

`messages` 不再持久化合成的 pinned 消息。发请求前临时物化 pins、anchor 和本回合 Skill 指导，
防止两份权威数据、重复注入及摘要污染。默认关闭新能力时保留既有可观察行为。

| 字段 | 语义 |
| --- | --- |
| `expected_seq` | 调用方预期的当前日志序号，用于冲突检查 |
| `through_seq` | 活动快照已吸收的日志水位线 |
| `anchor.covered_through_seq` | 锚构建器已经检查到的日志位置，不是最后一条相关证据位置 |
| `snapshot_revision` | 快照本身的修订号，保护不追加消息的 pin、锚或压缩更新 |

Usage 若不参与当前日志序号协议，不人为假设它会增加消息 seq。锚来源和当前代码状态还需显式引用
写入/验证证据，不能用消息水位线代替代码版本。

相同规范化输入和构建规则版本产生相同锚语义内容及 `content_hash`；`updated_at` 不参加确定性比较。
内容未变不重复生成内容记录。重建数据和时间元数据的规则在 M0 固定。

### 必须进入测试的审查重点

1. 两进程在相同日志 seq 下修改快照：不能静默覆盖；M0/M2 验证文件后端基本冲突，M4 扩展双后端压力/故障验证。
2. Skill 指导经过深拷贝与多次压缩：不能依赖对象身份剥离，M1 验证原文及摘要不残留。
3. 任务更新、pin 撤销和文件再次修改：旧目标及旧通过证据不能继续生效，M2 验证生命周期。
4. 超预算和恶意工具正文：不得静默删除硬约束或把低信任数据提升为规则，M1/M2 验证。
5. 迁移中断和切换后新写入：不能自动回退旧数据或误报成功，M5/M6 验证。

版本兼容审查同时检查读取和写入路径，不能只测试 `load_active_snapshot()` 抛错。

## 4. 任务锚与 Skill 组合规则

- 锚只从当前明确目标、用户显式 pin、宿主结构化约束、成功写入记录、工具实际状态及验证器结果构建。
- 第一版不使用模型提炼约束；任意工具正文、模型推测、历史摘要不能升级为可信指令。
- 首个用户任务只作为初始目标；提供显式目标更新/重置，`/new` 隔离锚，后续任务不能被旧目标永久约束。
- `/pin` 支持查看、添加、删除；用户撤销要生效。锚正文上限 2,000 字符，pins 另设条数和总字符上限，
  M0 明确具体限值；全部计入上下文总预算。
- 已接受的硬约束不能静默截断。无法容纳时拒绝新增或明确报告无法安全压缩，不允许无限堆积 pins。
- 文件清单可有界展示，但标明省略数量和证据位置；WriteJournal 不完整时明确显示。
- 验证结果绑定代码版本；新修改后，旧“通过”不能代表当前版本已验证。
- 保留来源和不完整状态；用户/工具文本的信任等级不能因进入锚而提升。
- 当前 Skill 指导可在本回合压缩后重新物化；回合结束后不得残留于普通历史、摘要或锚。
- 锚、pin、Skill 都不授予工具权限，不自动批准，不改变模型路由和 Sandbox。

## 5. 持久化与迁移规则

### SessionRepository 与文件后端

先定义覆盖实际调用点的 Protocol；保留 SessionStore 兼容入口，通过适配接入，避免无关重写。
M0 必须交付 `docs/acceptance/session-context/M0_CALL_SITES.md`，不能只列拟新增模块。
调用点清单至少包含：文件/函数、触发入口、读写接口、seq/revision 的来源与更新、目标签名/适配方式、
保留字段、对应回归测试及所属实施阶段。覆盖全仓库实际调用者，包括测试替身与内部包装器。

已确认必须检查的路径：REPL 初始化、消息持久化与回合结束保存；`commands/sessions.py` 的 `/new`、
`/resume`；`commands/context.py` 的 `/compact`；`SessionStore.create()`；
`JournalMixin.append_message()` 及内部快照推进；`load_active_messages()` 等兼容入口。
M1、M2、M6a、M6b 每次接线后更新清单，签名变更不能留下旧调用者静默丢字段或跳过 CAS。
文件后端继续用必要的文件锁与原子写入，整体保存 messages、pins、anchor 和快照版本。
保持日志尾恢复、Usage 幂等及中断工具协议修复语义。旧 messages 接口不能意外清空新增字段。
文件后端不宣称跨多个文件的数据库事务；错误语义和提交边界必须写明。

### SQLite 后端

- 使用标准库 sqlite3，表覆盖 `sessions / session_events / session_snapshots / usage_ledger`。
- 快照优先整体放入版本化 `payload_json`，无查询需求不重复拆列。
- 在事务中比较版本并提交相关修改，明确提交边界、忙等待上限和错误分类。
- CAS 冲突显式失败，不能无条件重试覆盖别人；文件后端锁和 SQLite 事务分别保护比较与写入。
- 审查数据库文件、父目录和 WAL/SHM 等辅助文件权限及生命周期，目标为私有目录 0700、数据文件 0600。
- 不用削弱耐久保证的配置换取测试通过。

### 切换、迁移和回滚

- files 默认，CLI 参数优先于环境变量；名称与项目现有配置习惯一致。
- 明确选择 SQLite 后故障必须报错，不能自动切回陈旧 files 继续写。
- 环境变量只选择介质，不同步数据；完整回滚需要验证过的反向迁移。
- 迁移包含预检、备份、写入隔离或一致快照、目标校验和显式切换，保留源数据。
- ID 冲突不静默覆盖；重复导入幂等；中断的目标不标成可用。
- 不复制正在写入的数据库主文件冒充一致备份；迁移策略须涵盖日志和辅助文件。
- 按规范化业务数据验证往返等价，不能只比较文件数；保留事件序号与修订关系。

## 6. 文件边界与测试映射

以下为拟修改/新增位置，M0 结合现状固定最小接口和精确签名；不得因本表存在就机械创建空模块。

| 阶段 | 产品代码边界 | 验证入口 |
| --- | --- | --- |
| M0 | 新 `src/core/active_context.py`、`src/persistence/repository.py`；现有 `checkpoint.py` / `session_store.py` / `journal.py`；调用点清单 | 新 `tests/test_active_context.py`、`tests/test_session_repository_contract.py`；旧 session/context 及版本降级测试 |
| M1 | `src/core/context.py`、`src/core/agent.py` | `tests/test_context.py`、`tests/test_skills_agent_injection.py` |
| M2 | 新 `src/core/task_anchor.py`；现有 agent、checkpoint 与写入/验证结果接点 | 新 `tests/test_task_anchor.py`；session/context 回归 |
| M3 | 新 `src/persistence/sqlite_repository.py`；文件后端适配 | 同一份 repository contract 双后端参数化 |
| M4 | 仓储事务、锁与快照修订控制 | 新 `tests/test_session_repository_concurrency.py`、`tests/test_session_repository_recovery.py` |
| M5 | 新 `src/persistence/migration.py`；CLI 迁移入口 | 新 `tests/test_session_migration.py` |
| M6a | 新 `src/commands/pin.py`；现有 CLI / REPL / context / status 命令与基础 Trace | 文件后端命令、Skill 和端到端接线测试 |
| M7a | 现有 `src/evaluation/` 下上下文专项验证入口；第一批产品文档 | 新 `tests/test_context_evaluation.py`、文件后端完整回归及机制报告 |
| M6b | 新 `src/persistence/factory.py`；CLI / REPL 后端配置及迁移接线 | 双后端命令、配置优先级和故障反馈测试 |
| M7b | 双后端对照与真实模型评测入口；综合交付文档 | 完整回归、双后端 parity 和脱敏在线评测报告 |

新评测入口可以定义本轮约束评分，但不得修改历史评分器或历史分母。E 阶段文件设计在触发时单独细化。

## 7. 阶段验收制度

主计划以本文件为准，避免另建重复状态源。阶段验收目录使用 `docs/acceptance/session-context/`。
下表文档是计划中的产物；只有启动对应阶段才创建，不将空模板当成验收成果。

**执行顺序：** `M0 → M1 → M2 → M6a → M7a（第一批）→ M3 → M4 → M5 → M6b → M7b（第二批）`。
M6、M7 各拆成 a/b 两个验收单元；对应 a、b 均通过后，其父阶段才算 `ACCEPTED`。
M0 保持独立，不因提前交付合并或削弱基线、版本及调用点审查。

| 阶段 | 当前状态 | 前置条件 | 验收文档（拟） |
| --- | --- | --- | --- |
| M0 | ACCEPTED | 现状核实 | `docs/acceptance/session-context/M0_BASELINE_AND_CONTRACT.md`、`docs/acceptance/session-context/M0_CALL_SITES.md` |
| M1 | ACCEPTED | M0 ACCEPTED | `docs/acceptance/session-context/M1_PINNED_CONTEXT.md` |
| M2 | ACCEPTED | M1 ACCEPTED | `docs/acceptance/session-context/M2_TASK_ANCHOR.md` |
| M6a | ACCEPTED | M2 ACCEPTED | `docs/acceptance/session-context/M6A_CONTEXT_COMMANDS.md` |
| M7a | NOT_STARTED | M6a ACCEPTED | `M7A_CONTEXT_RELEASE.md` |
| M3 | NOT_STARTED | M7a ACCEPTED | `M3_SQLITE_REPOSITORY.md` |
| M4 | NOT_STARTED | M3 ACCEPTED | `M4_CONCURRENCY_AND_RECOVERY.md` |
| M5 | NOT_STARTED | M4 ACCEPTED | `M5_MIGRATION.md` |
| M6b | NOT_STARTED | M5 ACCEPTED | `M6B_BACKEND_COMMANDS.md` |
| M7b | NOT_STARTED | M6b ACCEPTED | `M7B_EVALUATION_AND_RELEASE.md` |
| E1 | DEFERRED | M0–M7 ACCEPTED 且有跨会话重复调查证据 | `E1_EXPLICIT_MEMORY.md` |
| E2 | DEFERRED | E1 ACCEPTED | `E2_SKILL_MEMORY_RETRIEVAL.md` |
| E3 | DEFERRED | E2 ACCEPTED 且有检索质量或并发性能瓶颈证据 | `E3_OPTIONAL_INDEX.md` |

每份验收文档必须记录：目标/非目标、前置条件、验收项 ID、场景、操作、预期、测试命令、实际结果、
通过/失败/跳过数量、环境依赖、被验证代码版本、脱敏证据位置、审核结论、缺陷处理、未完成项和下一阶段准入。

统一验收记录行：`验收项 ID | 场景与预期 | 测试/命令 | 实际结果 | 证据 | 结论`。
文档以 `结论：ACCEPTED / BLOCKED / IN_PROGRESS` 收尾；验收记录与计划状态必须一致。

固定执行顺序：

1. 写阶段验收标准和关键失败测试，记录失败原因。
2. 最小实现并运行专项验证。
3. 审核差异和失败场景，修复阻断问题。
4. 填写真实验收文档，核对全部必选项。
5. 所有必选项通过且证据齐全，才能标 `ACCEPTED`、提交并推进。

未开始、进行中、阻塞、通过分别为 `NOT_STARTED / IN_PROGRESS / BLOCKED / ACCEPTED`。
任何必选项失败、跳过或缺证据都不能计为通过；可诊断修复，但不得跳过阶段。可选扩展用 `DEFERRED`，
不计入核心已完成范围。已验收接口改变时重新执行受影响验收并更新文档。

通过后自动继续下一阶段，不必例行重复询问；真实阻塞或需扩大范围时明确说明。不得只创建空白验收模板。

第一批交付只证明其已验收范围，不能把 M3–M7b 或 E 阶段标成完成。M7a 通过后可准备独立发布材料；
`main` 仍按最终审核约定执行，准备发布本身不阻塞已授权的后续阶段开发。

## 8. 核心阶段 M0–M7

### M0：基线与共享契约（ACCEPTED）

- [x] 创建验收目录，在 M0 文档中分别记录本地测试证据、CI 运行证据和两者覆盖差异，复核历史 4 项失败。
- [x] 本地证据包含代码 SHA、系统/Python/依赖版本、命令、结果和失败用例；CI 证据包含 SHA、run URL/ID、
  工作流/环境、通过步骤和跳过范围。认定“既有失败”须有同环境旧提交的复现或可核对日志，否则标待归因。
- [x] 固定 v3 ActiveContext/Repository 的字段、接口签名、序号、任务生命周期、预算、升级隔离和错误语义。
- [x] 完成 `M0_CALL_SITES.md`，逐项映射读取、写入、兼容包装器、内部快照推进与测试替身。
- [x] 先写旧格式、空锚、旧接口、日志尾恢复、未知版本及旧程序降级的失败测试，再实现兼容契约。
- [x] 专项验证、差异审核，完成 `M0_BASELINE_AND_CONTRACT.md` 后才推进。
      验收文档：`docs/acceptance/session-context/M0_BASELINE_AND_CONTRACT.md`（ACCEPTED）、
      `docs/acceptance/session-context/M0_CALL_SITES.md`（ACCEPTED）。
      复审（`ec991bb..46b49dd`）发现的 5 项问题已修复并回归，见验收文档 §5b。

**验收标准 M0-A：** 基线结果可复现，失败均有证据且相关阻断已处理；无锚/无 pin 的既有行为在契约集等价；
日志尾不丢不重；旧接口不擦除新字段；水位线与修订号职责、具体预算和签名均确定。

**M0-B：** 新写入为 v3，旧读取器拒绝 v3，版本错误预检先于新代码的写入；旧写路径风险和隔离方案有测试/证据；
失败后的持久化状态符合预期，不宣称所有旧二进制写接口已获得保护。

**M0-C：** 调用点清单完整且有测试映射；历史 4 项失败与当前 CI 分开取证，没有“CI 绿所以本地也绿”的推断。

### M1：有界 pinned context 与压缩（ACCEPTED）

- [x] 先写多次压缩、工具配对、超预算和 Skill 原文/摘要残留测试。
- [x] 实现结构化 pins、总预算和请求前物化，保持 system 安全规则及工具协议。
- [x] 验证 Skill 深拷贝后单回合生命周期，完成 `M1_PINNED_CONTEXT.md`。
      验收：`docs/acceptance/session-context/M1_PINNED_CONTEXT.md`（ACCEPTED）。

**验收标准 M1-A：** 固定语料连续至少 20 次压缩，已接受 pins 保留率 100%；工具协议无孤立或缺配对；
超预算明确失败；工具正文不能成为 pin 或授权；下一回合无上回合 Skill 原文及摘要残留。

### M2：TaskAnchor 与文件后端恢复（ACCEPTED）

- [ ] 先写确定性、重启、目标切换、pin 撤销、写入证据不完整和旧验证失效测试。
- [ ] 实现有界锚、证据引用、更新/重置和覆盖水位线，接入文件快照。
- [ ] 验证文件后端相同 seq 的 pin/锚/压缩竞争、进程中断恢复和兼容包装器；同步调用点清单。
- [ ] 验证恢复后模型实际看到的输入，完成 `M2_TASK_ANCHOR.md`。

**验收标准 M2-A：** 固定输入语义内容/哈希一致率 100%；重复压缩与重启保持锚；不完整记录显式标识；
新修改使旧通过证据失效；切换任务/撤销 pin 后无旧约束误用；锚构建额外模型请求为 0。

**M2-B：** 第一批需要的文件后端修订号 CAS、原子快照和恢复通过专项验证，不能把全部保护延后到 M4。

### M6a：第一批上下文交互与基础观测（ACCEPTED）

- [x] 先写 `/pin` 查看/添加/删除、目标查看/更新/重置、`/new`、`/resume`、`/compact` 的端到端测试。
- [x] 在文件后端接通命令和 REPL，增强 `/context` 展示锚、pins、预算、水位线和修订号。
- [x] 接入 `context_pinned / anchor_updated / context_compacted / session_cas_conflict /
  session_snapshot_saved / session_storage_failed` 基础事件，验证不泄露正文及凭据。
- [x] 核实 Skill/Workflow/多 Agent 支持矩阵；未支持组合明确报告，更新 M0 调用点清单。
- [x] 完成 `M6A_CONTEXT_COMMANDS.md`；后端工厂、切换和迁移事件保留给 M6b。

**验收标准 M6a-A：** 用户可实际设置、查看、撤销和恢复约束；关闭锚后兼容；正常退出或重启后展示与模型
实际输入一致；旧任务/Skill 无残留；不支持组合不静默忽略 pin；存储失败不显示保存成功。

### M7a：第一批专项验收与可展示交付（在 M3 前执行）

- [ ] 冻结文件后端的上下文离线语料、验收项和预期；覆盖长会话多次压缩、撤销/改目标、旧验证失效、
  提示注入、重启、旧版兼容和基本快照竞争。
- [ ] 完成 M0–M2 与 M6a 的端到端演示、完整离线回归、构建、文档和差异审核。
- [ ] 用 `M7A_CONTEXT_RELEASE.md` 记录第一批范围、机制指标、命令演示、升级/回退方法及尚未实施的第二批。
- [ ] 如本批运行真实模型 anchor off/on，对照前固定预算/评分并完成离线前置；否则明确记为未执行，
  该行为收益验证保留为 M7b 必选项，不影响已经预先限定为离线机制验收的第一批结论。

**验收标准 M7a-A：** 所有第一批必选项、全量 pytest、Ruff、格式、构建和秘密检查通过；可演示设置约束→
多次压缩→重启恢复→变更/撤销目标；无新增运行依赖。文件后端安全验证不能因 SQLite 尚未实施而跳过。
报告只宣称已证实的约束保留和恢复机制；未经真实模型对照不得宣称降低模型漂移率或提高任务成功率。
M7a 通过代表第一批可发布，不代表 M6/M7 父阶段或整个融合方案已经验收。

### M3：SQLite SessionRepository

- [ ] 确认第一批 M7a 已验收，复用其数据契约、命令和测试，不重复实现。
- [ ] 将相同契约测试参数化为 files/sqlite，先证明 SQLite 缺失行为。
- [ ] 实现会话、事件、完整快照、Usage 和元数据操作，保持统一错误语义和兼容接口。
- [ ] 验证事务边界、幂等和工作区隔离，完成 `M3_SQLITE_REPOSITORY.md`。

**验收标准 M3-A：** 已列举双后端契约用例通过率 100%；Usage 不重复累计；工作区不混读；
消息、审批状态和活动上下文语义等价；关键业务更新具有明确的原子提交结果。

### M4：并发和崩溃耐久

- [ ] 先用真实多进程构造日志竞争和相同 seq 的仅快照竞争，定义受控故障注入点。
- [ ] 完成版本冲突与事务/落盘边界处理；验证超时、权限和资源清理。
- [ ] 执行恢复及 integrity_check，完成 `M4_CONCURRENCY_AND_RECOVERY.md`。

**验收标准 M4-A：** 两类竞争均无静默覆盖，冲突方明确失败；崩溃仅恢复完整旧状态或完整新状态；
测试覆盖的进程崩溃不丢已确认提交数据；integrity_check 通过；权限、锁超时和连接/临时文件清理符合设计。
不得把进程 kill 测试描述为断电或硬件故障证明。

### M5：迁移和一致性

- [ ] 先测试旧数据、尾消息、pin、anchor、Usage、元数据及版本关系的往返、重复和中断场景。
- [ ] 实现预检、备份、一致读取、目标校验与显式切换，覆盖切换后新增数据的反向迁移。
- [ ] 审核失败路径，完成 `M5_MIGRATION.md`。

**验收标准 M5-A：** 固定迁移语料的规范化业务内容往返相等；重复导入无重复事件/Usage；
中断不损坏源数据、半成品不可用；目标冲突报错；SQLite 新写入可完整迁回 files 后再切换。

### M6b：后端切换、迁移接线和观测补齐

- [ ] 先写后端配置优先级、迁移切换、恢复和数据库故障反馈的端到端测试。
- [ ] 工厂接入全部实际入口，将 M6a 命令参数化为双后端，`/context` 补充实际后端信息。
- [ ] 复用 M6a 的基础 Trace，补齐 `session_migrated` 等后端事件，只保留安全元数据。
- [ ] 回归 M6a 支持矩阵和命令行为，核对 M0 调用点清单，完成 `M6B_BACKEND_COMMANDS.md`。

**验收标准 M6b-A：** 双后端配置、新建、恢复、迁移全链路通过；Trace 不泄露凭据/私有正文；
故障不报成功或回退陈旧数据；files 默认及锚关闭路径兼容；M6a 既有验收无回归。

### M7b：双后端综合评测与第二批交付

- [ ] 先冻结任务、约束、评分器、模型/Skill 设置、重试和时间预算，再执行对照。
- [ ] 对照 anchor off/on 与 files/sqlite；两组 Skill 设置一致，不能混合归因。
- [ ] 复用 M7a 场景并扩展双后端、并发及迁移；覆盖多次压缩、无关干扰、目标更新、约束撤销、验证失效、重启与提示注入。
- [ ] 完成离线门槛后才执行真实模型评测；审核所有成功、失败、超时和服务错误。
- [ ] 完整回归、文档核对与发布审查，完成 `M7B_EVALUATION_AND_RELEASE.md`。

**指标：**

- 机制：约束保留率、版本正确率、恢复完整率、后端契约通过率。
- 行为：任务漂移率、严格/首次/重复成功率、重复工具调用、端到端耗时、总 Token、每成功任务 Token。
- 安全：越权、跨工作区读取、秘密泄露、静默覆盖次数。
- 性能：压缩、快照保存和恢复延迟，标明样本数及环境。

**验收标准 M7b-A：** 离线机制与安全必选项全部通过；全量 pytest、Ruff、格式、构建、敏感信息检查和
Git diff 审核通过；在线评测按预先定义的约束与实际结果判断漂移，不能用“锚还在输入里”证明不漂移。
失败不得重跑到成功后隐藏。基线漂移为 0 时报告本次无法证明改善，不能虚构收益。
模型或 Docker 不可用时记录准确阻塞和未覆盖项；对应必选验证未执行不能标 M7b 或 M7 父阶段 ACCEPTED。
M7a 如已有同版本且配置等价的在线证据可经审查复用；实现/模型/评分变化则重新验证，不无理由重复跑全量历史评测。

## 9. 条件扩展 E1–E3

核心验收通过且存在跨会话重复调查证据才进入 E1；不满足时记录触发条件，结束核心交付。
每个 E 阶段仍需先写具体测试与验收标准、实施、出具验收文档、通过后推进。

### E1：显式跨会话知识

- [ ] 设计独立 MemoryRepository、SQLite 文件和迁移版本，不扩展 SessionRepository 承担检索。
- [ ] 只保存用户确认的项目规范、架构决策、验证方案及偏好；通过 `/remember /forget` 和查询命令管理。
- [ ] 实现 Scope、来源、版本、去重、冲突、失效及关键词检索；FTS5 不可用时明确处理。
- [ ] 原始对话、完整 Trace、任务锚和工具输出不直接批量导入，完成 `E1_EXPLICIT_MEMORY.md`。

**验收标准 E1-A：** 固定安全语料中跨工作区泄漏、未授权写入、秘密样本持久化均为 0；写入幂等，
冲突不静默覆盖；删除后正文与索引不可检索；关闭后原 Agent 可用；检索质量、失败样本和耗时可复核。

### E2：Skill 按需检索知识

- [ ] Skill 只能读取允许 Scope/类型内的有界记忆，返回内容保持低信任参考等级。
- [ ] 不因检索增加权限，不把检索内容自动保存为新记忆，不破坏单回合生命周期。
- [ ] 运行 memory off/on 对照，完成 `E2_SKILL_MEMORY_RETRIEVAL.md`。

**验收标准 E2-A：** 错误、过期和恶意记忆不能绕过宿主控制；固定回归无新增阻断退化；报告成功率、
重复调查、Token、检索质量和耗时；只有观察到预先约定收益才保留默认推荐配置。

### E3：Embedding / Redis（可选）

- [ ] 先证明关键词质量或实际并发性能不足，再确定接口、索引更新、删除、版本和重建协议。
- [ ] Redis 仅作可重建索引或缓存，SQLite 为事实源；连接凭据只从环境变量获取。
- [ ] 验证故障回退权威存储和写失败反馈，完成 `E3_OPTIONAL_INDEX.md`。

**验收标准 E3-A：** 无索引时产品仍可用；失败不报成功；删除和版本一致性验证通过；增加组件带来的
收益可复核；不得把同机多进程测试描述为分布式系统验证。不为增加技术栈而启动本阶段。

## 10. 验证、审核和 Git 交付

- 阶段内用有意义的 RED→GREEN 测试和专项回归；共享契约、持久化接线及最终交付执行完整回归。
- 基础命令：`uv run --frozen pytest -q`、`uv run --frozen ruff check src tests`、
  `uv run --frozen ruff format --check src tests`、`uv build --no-config`、`git diff --check`。
  Docker、故障注入和模型评测的具体入口在所属阶段验收文档中固定，不能以默认跳过代替执行。
- 检查敏感信息时只报告通过/问题位置，不输出秘密；原始证据保存在不跟踪的运行目录，验收文档仅含脱敏结果。
- 每个稳定阶段审核通过、阻断修复并有验收证据后，以明确文件列表提交、推送 `optimize`，不广泛暂存无关改动。
- 计划文档更新单独检查内容、链接和差异即可，不为了纯文档修改重复跑完整模型评测或把基线写成新通过。
- 阶段提交不代表发布。第一批 M7a 通过即可按其明确范围准备独立发布材料；第二批 M7b 通过再准备完整核心发布。
  两批都保留 main 最终审核约定：经用户最终审核再合并并推送 `main`，不因保存计划自动实施或合并。
- 每次汇报当前阶段、通过证据、阻塞、验收文档路径和下一步。不得将未执行的检查写成通过。

---

## 历史附录：Skill MVP P0–P6 原始计划

以下保留先前需求、文件拆分和提交记录，作为已交付 Skill 的来源说明；不再次执行 P0–P6。
其“全部完成”指历史代码交付记录，不代表真实模型 A/B 或当前全量测试已经重新验收。
本轮范围、顺序和验收制度以正文为准；原 Skill 的“不新增长期记忆”仅描述当时 MVP 范围。

> **执行约定：** 全部阶段在 `optimize` 分支开发；每阶段先写失败测试（TDD）→ 最小实现 →
> 全量验证（pytest / ruff check / ruff format --check / build）→ 审查 → 提交并推送 `optimize`。
> **未经用户最终审核不合并 `main`。** 到达合并点时停下来提示用户。

**目标：** 用声明式、受权限约束的 Skill MVP 提升 Agent 对典型开发任务的执行稳定性，
同时复用现有上下文、持久化、审计、用量与权限组件，不引入新记忆系统或任意代码加载。

**明确不做：** 不新增长期语义记忆 / Redis / 向量库；不重复实现会话持久化；Skill 不携带或
执行任意 Python；不新增 LLM 选择；不改评分器；不提交 Key/URL/原始 Trace/Session/运行产物；
不把面试或学习材料提交进仓库。

**已确认的设计决策（本计划默认采用）：**
1. **默认关闭**：Skill 默认 `off`；需 `/skill auto` 或 `/skill <id>` 显式启用，保证“关闭即与现有行为一致”。
2. **受限注入**：Skill 指导作为**紧邻 system prompt 的受限消息**在**单回合内注入**，
   `Agent.run` 结束（`finally`）按对象身份剥离，绝不落入 `save_active_messages` 或后续回合。
3. **仅单 Agent 路径**：`provider.workflow is None` 时才激活，与现有 loop/write 软提示同护栏；
   多模型 Workflow 不激活。

**Skill 优先级（固定、确定性、零 LLM）：**
`用户显式选择 > 精确任务类型 > 确定性关键词评分 > 不选择`

**安全边界：** Skill 只产出“受限文本 + 元数据”；工具必须 ⊆ 当前 Policy 允许集合；`read_only`
不得含写/命令工具；不扩大权限、不自动批准、不换 Sandbox、不改模型路由；完成后仍由现有
`CompletionVerifier` 验收。

**可观测：** 复用 `TraceWriter`，事件 `skill_discovered / skill_selected / skill_rejected /
skill_started / skill_completed / skill_failed`，字段 `skill_id, skill_version, selection_method,
selection_reason, run_id, step, duration_ms, result_status`；只记元数据，不写完整 prompt。

---

### 历史阶段计划

#### P0 骨架与 Manifest 校验
- 新增 `src/skills/__init__.py`、`errors.py`、`models.py`、`loader.py`。
- 严格 Manifest：`id, version, description, task_types, trigger_keywords, required_tools,
  read_only, max_prompt_chars, prompt_file`；拒绝未知字段/错类型/重复 id/坏版本。
- 常量：`SKILL_ID_RE`、`SEMVER_RE`、`PROMPT_FILE_ALLOWED`、硬上限（文件 ≤ 8 KiB、prompt ≤ 8000 字符）。
- 路径防护：仅限内置目录、无 `..`、无绝对路径、无符号链接；`prompt_file` 白名单。
- 测试：`tests/test_skills_models.py`、`tests/test_skills_loader.py`。

#### P1 注册表与确定性选择
- 新增 `src/skills/registry.py`、`selector.py`。
- 内置发现 + id/version/文件名白名单 + 去重；`list()` 稳定排序。
- 选择优先级实现 + 阈值 + 并列即不选；负例（无关/弱命中）不激活。
- 测试：`tests/test_skills_registry.py`、`tests/test_skills_selector.py`（准确率 100%、一致性 100%）。

#### P2 执行、Trace 与 Agent 注入
- 新增 `src/skills/executor.py`：工具子集校验、guidance 生成、生命周期 Trace。
- 修改 `src/core/agent.py`：`run()/_run()` 增加 `skill_guidance`；注入 + `finally` 剥离。
- 测试：`tests/test_skills_executor.py`、`tests/test_skills_security.py`（越权 0、穿越 0、Trace 完整 100%）。

#### P3 终端接口
- 新增 `src/commands/skills.py`：`/skills`、`/skill auto|<id>|off`（只接受 id，不接受路径）。
- 修改 `src/commands/__init__.py`（注册）、`src/commands/status.py`（显示当前 Skill/方式/版本）、
  `src/interfaces/repl.py`（构建 registry、run_turn 接线、保存运行时状态）。
- 测试：`tests/test_skills_commands.py`。

#### P4 内置 Skill 资源与打包
- 新增 `src/skills/builtin/repository-investigation/{skill.toml,prompt.md}`（只读定位）。
- 新增 `src/skills/builtin/python-test-repair/{skill.toml,prompt.md}`（复现→定位→最小改→专项→回归；
  写权限仍由 Policy/Approval 决定）。
- 修改 `pyproject.toml`：hatch `packages` 增 `src/skills`，确保资源随 wheel 打包；重装 editable。
- 测试：`tests/test_skills_builtin.py`（两 Skill 合法、只读约束、prompt 上限）。

#### P5 集成与量化
- `tests/test_skills_integration.py`：`/skill off` 与基线行为一致；Host/Docker 兼容；
  端到端一轮注入与剥离。
- 量化（复用现有 `src/evaluation`，不新增评分器）：Skill off vs on 的严格成功率、首次成功率、
  总 Token、每成功 Token、端到端耗时、工具重复调用数、权限拒绝/越权次数；产出对照报告（不入库）。

#### P6 文档与收口
- 新增 `docs/SKILLS.md`；更新 `README.MD`、`docs/PROJECT_SCOPE.md`、`docs/ARCHITECTURE.md`；
  新增迭代文档 `docs/iterations/ITERATION_041_SKILLS_MVP.md`。
- 全量 `pytest` / `ruff check` / `ruff format --check` / `uv build --no-config`。

---

### 历史验收清单（不代表本轮已验证）
- 固定选择用例准确率 100%；负向误激活 0；同输入重复选择一致率 100%。
- 越权成功 0；路径穿越 / 任意文件加载成功 0；Trace 生命周期完整率 100%。
- Skill 关闭后与现有 Agent 行为兼容；Host / Docker 行为兼容。
- Manifest 严格字段校验；未知字段/重复 ID/非法工具名拒绝；无 Key/URL 凭据。
- 全量 pytest、ruff、format、build 通过。

### 历史进度（提交记录）
- [x] P0 骨架与 Manifest 校验（0325a2b）
- [x] P1 注册表与确定性选择（436700f）
- [x] P2 执行、Trace 与 Agent 注入（d77dca9）
- [x] P3 终端接口（237de7b）
- [x] P4 内置 Skill 资源与打包（a9624e6）
- [x] P5 集成与离线指标（290f2c0）；真实模型 Skill on/off 对照仍以迭代文档为准，未在本轮验证。
- [x] P6 文档与收口（edfceef）
- [x] 截至本次本地核对，Skill 提交已包含在 `main` 和已知远程跟踪分支；历史等待合并状态已过时。
- [ ] 当前完整验证、历史 4 项失败和真实模型 A/B 状态：由本轮 M0 复核，不据历史勾选宣称通过。
