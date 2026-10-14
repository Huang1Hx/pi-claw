# M7a 验收：第一批上下文机制验收与可展示交付

- 阶段：M7a（第一批专项验收与可展示交付）
- 日期：2026-10-14
- 前置：M0、M1、M2、M6a 均 ACCEPTED
- 主计划：`newplan.md` §8(M7a)
- 结论：**ACCEPTED**（第一批可发布；合并 `main` 仍需用户最终审核）

## 1. 交付范围（第一批）

- **文件后端**上下文一致性：v3 `ActiveContext` 契约、`SessionRepository` 端口、有界 pinned context、
  确定性任务锚、修订号 CAS（M0/M1/M2）。
- **交互与观测**：`/pin`、`/goal`、增强 `/context`、`/compact`、回合内 pins/锚临时注入、基础 Trace（M6a）。
- **明确不在本批**：SQLite 后端与迁移（M3–M5）、后端工厂/切换双后端（M6b）、真实模型 A/B 与完整评测（M7b）。

**第一批不代表整个融合方案完成**：M6/M7 父阶段需 a、b 两个单元均通过才 ACCEPTED。

## 2. 冻结离线语料（`context-offline-v1`）

语料文件：`tests/test_context_offline_corpus.py`；版本常量 `CORPUS_VERSION = "context-offline-v1"`。
全部确定性、纯离线：只用 `ScriptedProvider` 与真实 REPL/工具/持久化路径；`tests/conftest.py` 全局禁止真实网络。

| 覆盖轴 | 用例 | 冻结预期 |
| --- | --- | --- |
| 长会话多次压缩 | `test_corpus_constraint_lifecycle_through_compactions` | 12 次压缩后约束保留 2/2 |
| 提示注入 | 同上 + `test_corpus_injection_never_reaches_request_as_instruction` | 注入文本成为约束/指令 0 次 |
| 写入→锚→重启恢复 | `test_corpus_write_record_anchor_and_restart_recovery` | 重启后目标/约束/锚齐全，展示与临时块一致 |
| 只读工具不误报改动 | `test_corpus_read_tool_is_not_counted_as_changed_file` | `read_file`/`search_text` 不计入“已改文件” |
| 改/撤销目标 | `test_corpus_goal_change_and_reset` | 改目标即时生效；重置清空目标保留约束 |
| 旧验证失效 | `test_corpus_verification_invalidation_is_frozen` | 指纹变化后标注“失效”，哈希改变 |
| 旧版兼容 | `test_corpus_legacy_v2_snapshot_upgrades` | v2 升级为 v3，pins/goal 为空，历史不丢 |
| 基本快照竞争 | `test_corpus_basic_snapshot_race_rejects_second_writer` | 同修订号第二个写者被拒绝 |

## 3. 机制指标（离线，仅宣称已证实的机制）

| 指标 | 值 | 证据 |
| --- | --- | --- |
| 接受约束跨 12 次压缩保留率 | 100%（2/2） | 语料用例 1 |
| 重启后目标/约束/锚恢复 | 100%（1/1） | 语料用例 3 |
| 注入文本提升为约束/指令 | 0 | 语料用例 1、2 |
| 同修订号并发写者拒绝率 | 100%（1/1） | 语料用例 7 |
| 锚内容确定性（同输入同哈希） | 100% | `tests/test_task_anchor.py` |
| 临时块泄漏进持久化消息 | 0 | 语料用例 1、3 |
| 锚构建额外模型请求 | 0 | `tests/test_task_anchor.py` |

**不得据此宣称**降低模型漂移率或提高任务成功率——真实模型 anchor off/on 对照**本批未执行**，
按计划保留为 M7b 必选项（见 §6）。

## 4. 命令演示（真实输出，/goal · /pin · /context · 重启 · reset）

```text
>>> /goal 修复计费回归
已设置当前目标：修复计费回归
>>> /pin 不要改动评分器
已添加约束 p1：不要改动评分器
>>> /pin 必须运行 pytest
已添加约束 p2：必须运行 pytest
>>> /context
活动消息=1 字符=1055 tokens≈264
水位线：through_seq=1 修订号：snapshot_revision=6
预算：messages≤40 chars≤80000 pins≤32 条/总长≤1200 字符
目标：修复计费回归
约束：p1=不要改动评分器；p2=必须运行 pytest
任务锚：
目标：修复计费回归
约束：不要改动评分器；必须运行 pytest

>>> 重启后 /context
（目标、约束、任务锚与上个会话一致，水位线/修订号一致）

>>> /goal 新目标
已设置当前目标：新目标
>>> /goal reset
已重置当前目标与任务锚。
>>> /context
约束：p1=不要改动评分器；p2=必须运行 pytest
任务锚：
约束：不要改动评分器；必须运行 pytest

>>> /pin -d p1
已删除约束 p1。
```

演示覆盖“设置约束 → （多次压缩）→ 重启恢复 → 变更/撤销目标”的完整链路。

## 5. 端到端与完整验收证据

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 全量离线回归 | `.venv/bin/python -m pytest -o addopts="" -q` | `4 failed（既有沙箱限制）, 972 passed, 3 skipped` |
| 语料专项 | `pytest -q tests/test_context_offline_corpus.py` | `9 passed` |
| 静态检查 | `ruff check src tests` | 通过 |
| 格式 | `ruff format --check src tests` | 177→178 files formatted，通过 |
| 构建 | `uv build --no-config` | 生成 `pi_claw-0.1.0.tar.gz`、`pi_claw-0.1.0-py3-none-any.whl` |
| 秘密检查 | `git grep -nIE "(sk-[A-Za-z0-9]{16,}|api[_-]?key[:=]|Bearer …)"` 覆盖 278 个跟踪文件 | 仅测试假凭据与文档，未发现真实密钥/凭据；`.env` 未被跟踪 |

- 4 项失败为既有环境相关（`test_sandbox_lifecycle` 需 `ps`、`test_terminal_experience` 需网络），
  失败集合自 M0 起未变，与本批改动无关；CI（Linux）侧为通过。
- 文件后端安全验证未因 SQLite 未实施而跳过：越权/路径穿越/预算与 CAS 契约在 M0–M2 与语料中均已覆盖。

### 5.1 CI 证据

- 本批最后一次提交推送后由 GitHub Actions `ci` 复核（见本文件末尾“CI 记录”小节）。

## 6. 未执行 / 未覆盖（如实记录）

- **真实模型 anchor off/on 对照：未执行。** 需要固定预算/评分与离线前置，属 M7b 必选项；
  本批结论仅限“约束保留与恢复机制”，不含模型行为收益。
- SQLite/迁移、双后端切换、迁移失败回滚（M3–M5、M6b）。
- Workflow 多模型模式下 pins/锚不支持（显式告警），团队多 Agent 模式未纳入本批。

## 7. 升级与回退

- **升级**：本批只新增 v3 可选字段 `goal` 与 v3 快照能力。旧 v2/v3 快照自动升级；无数据库、无迁移。
- **回退**：文件后端行为对既有调用点默认不变；回退只需 `git revert` 本批提交。旧程序读 v3 快照会**显式失败**
  （不静默当 v2），如需回退到 v2 语义须先清理活动快照，`journal` 原始历史始终保留。
- **无新增运行依赖**：不引入 Redis、向量库、外部服务或新第三方包。

## 8. 自审记录（两次）

**自审 #1（代码接线，`23ec4a5..工作区`）**

- 发现 1（已修，真实缺陷）：命令修改 goal/pins 后未即时重建锚，导致 `/context` 展示落后于实际注入。
  已在 `/goal`、`/pin` 写入后调用 `sync_anchor()`，命令层展示与请求一致。
- 发现 2（已修，真实缺陷）：`WriteJournal` 仅存于进程内，重启后重建锚会丢失“已改文件”证据（并覆盖旧锚）。
  已改为 `_collect_changed_files()`：合并进程内日志与持久化消息中成功的写工具结果，重启后锚不丢证据。
- 发现 3（已修）：写入路径属性名错误（`relative_name` → `WriteRecord.path`）。
- 发现 4（已修，真实缺陷）：`_collect_changed_files` 起初只按 `ok+path` 判定，且路径实际嵌在 `output.path`，
  会漏报真实写入、并可能把只读工具结果误当改动。已按写工具白名单（`write_or_replace`/`apply_patch`）
  过滤并从 `output.path` 取路径；新增只读工具负例用例。原重启用例断言过宽（历史工具消息也含路径），
  已收紧为只检查临时块内容。

**自审 #2（语料与文档一致性）**

- 逐条核对：语料 8 项覆盖计划要求的 7 个轴；指标与用例一一对应；命令演示为真实运行输出。
- 诚实性核对：未把真实模型对照写成已执行；4 项既有失败如实标注为环境相关；构建/秘密检查给出命令与结果。
- 遗留（非缺陷，记入 M7b/M6b）：Workflow 检测依赖运行时 `provider.workflow` 状态，工作流首次触发前可能滞后一回合。

## CI 记录

- Run：`ci` **38566617780**，提交 SHA `d03d4ec316bc9d693e4bdc920df52f8b758aa7d5`（`optimize`），
  触发/结论：push / **success**（Ubuntu，Python 3.12；Lint、Format、Compile、Test、Build 步骤通过）。
- URL：https://github.com/Huang1Hx/pi-claw/actions/runs/38566617780
- 说明：CI（Linux）通过不改变本地 4 项 macOS 环境相关失败的既有判定；两者证据分开记录，不互相推断。
