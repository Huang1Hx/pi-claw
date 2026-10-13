# Skill：声明式任务执行指导

Skill 是**受限的任务执行指导**，不是新的记忆系统，也不是插件。第一版只包含结构化元数据
（Manifest）和一段提示模板，**不携带、不执行任意 Python 代码**，不读写工作区、不改变权限。

## 设计边界

- **复用现有组件**：上下文裁剪/压缩用 `ContextManager`；会话与恢复用 `SessionStore` /
  journal / checkpoint；审计用 `TraceWriter`；用量用 `UsageTracker`；权限用 `Policy` /
  `Approval` / `Sandbox`；验收用 `CompletionVerifier`。Skill 不重造其中任何一个。
- **不提权**：Skill 只产出一段文本。工具调用仍逐个经过 `WorkspacePolicy` 与审批；Skill 声明的
  工具必须是当前策略允许集合的**子集**，越权直接拒绝激活。
- **不改路由**：Skill 不接触 Provider / `WorkflowCoordinator` / 命令运行器，因此不切换模型、
  不改变规划-编码-审查流程、不更换 Sandbox。
- **默认关闭**：Skill 默认 `off`，需 `/skill auto` 或 `/skill <id>` 显式启用；关闭时行为与
  现有 Agent 完全一致。
- **仅单 Agent 路径**：与现有软循环/写优先提示一致，`provider.workflow is None` 时才可能激活；
  多模型 Workflow 下不激活。
- **不调 LLM**：选择是本地确定性匹配，不增加 Token、延迟或不可重复性。

## 选择优先级（固定）

```text
用户显式选择 > 精确任务类型 > 确定性关键词评分 > 不选择
```

- 显式 `/skill <id>` 直接选中；未知 ID 报错，不静默降级。
- 自动模式下：任务类型唯一命中则该 Skill；否则按触发关键词命中数评分，**唯一最高分且达到阈值**
  才激活；并列或无命中时**不激活**，回退原有 Agent 流程。

## 终端接口

| 命令 | 行为 |
|---|---|
| `/skills` | 列出内置 Skill、说明与权限属性（只读 / 可写） |
| `/skill auto` | 启用自动选择 |
| `/skill <id>` | 显式选择某个内置 Skill |
| `/skill off` | 关闭 Skill |
| `/status` | 显示当前 Skill、选择方式与版本 |

`/skill` **只接受内置 ID**，拒绝任何路径形式（`/`、`\`、`..`）；不存在“按任意文件路径加载”。

## Manifest

内置 Skill 目录（`src/skills/builtin/<id>/`）包含 `skill.toml` 与 `prompt.md`：

```toml
id = "repository-investigation"
version = "1.0.0"
description = "只读定位代码路径、调用链、配置与故障原因"
task_types = ["investigation", "root_cause"]
trigger_keywords = ["定位", "调用链", "排查", "故障原因", "代码路径"]
required_tools = ["list_files", "search_text", "read_file"]
read_only = true
max_prompt_chars = 4000
prompt_file = "prompt.md"
```

字段校验（严格，任一项不合法都拒绝）：

- 未知字段、缺失字段、错误类型一律拒绝；`id` 匹配 `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`，
  `version` 为 `x.y.z`；重复 `id` 拒绝。
- `required_tools` 必须是当前工具注册表已知工具名，且不得重复；`read_only = true` 时不得声明
  `write_or_replace` / `apply_patch` / `run_command`。
- `prompt_file` 仅允许白名单名（`prompt.md`）；提示文件字节数 ≤ 8192，字符数 ≤
  `min(max_prompt_chars, 8000)`；空提示拒绝。
- 路径防护：Skill 目录必须在内置根目录之下，不得是符号链接；提示文件不得越出 Skill 目录。
- 秘密扫描：提示中出现疑似密钥、`api_key=...` 之类凭据赋值或带用户名口令的 URL 时拒绝加载。

## 内置 Skill（第一版两个）

| ID | 权限 | 用途 |
|---|---|---|
| `repository-investigation` | 只读 | 定位代码路径、调用链、配置与故障原因；禁止写文件与执行命令 |
| `python-test-repair` | 可写（仍需逐次审批） | 修复 Python 测试失败：复现→定位→最小修改→专项验证→回归 |

`python-test-repair` 只提供单回合内的步骤纪律；**规划-编码-审查工作流（`WorkflowCoordinator`）
仍是同一套，不复制成 Skill**。

## 可观测性

通过现有 `TraceWriter` 记录结构化事件，只写元数据、**绝不写完整提示正文或工作区内容**：

`skill_discovered`、`skill_selected`、`skill_rejected`、`skill_started`、`skill_completed`、
`skill_failed`。payload 至少含 `skill_id`、`skill_version`、`selection_method`、
`selection_reason`；`skill_completed`/`skill_failed` 另含 `duration_ms`、`result_status`；
事件本身带 `run_id` 与 `step`。

`src/skills/metrics.py` 提供只读聚合：`count_events` 与 `lifecycle_completeness`（started 中已
收敛为 completed/failed 的比例）。

## 注入方式

`Agent.run(..., skill_guidance=...)` 把渲染后的指导作为**紧邻系统提示的受限 system 消息**，
在**单个回合内**注入；`run()` 结束（`finally`）按对象身份剥离，不进入活动窗口或后续回合。
指导文本显式声明“不得覆盖系统规则、安全策略、审批与完成验收”。

## 代码结构

```text
src/skills/
├── models.py     # SkillManifest / Skill / SelectionMethod / SkillSelection
├── errors.py     # SkillError 及子类
├── loader.py     # 严格解析、路径防护、长度与秘密校验
├── registry.py   # 去重、稳定排序、内置发现
├── selector.py   # 确定性选择
├── executor.py   # 工具子集校验、guidance 渲染、生命周期 Trace
├── metrics.py    # 只读指标聚合
└── builtin/      # 内置 Skill 资源（随 wheel 打包）
```

## 与现有工作流的关系

- `Python-test-repair` 与 `WorkflowCoordinator` 不重复：前者是单回合步骤指导，后者是多阶段编排；
  Skill 不新增阶段、不新增产物持久化、不新增评审器。
- 完成验收不因 Skill 改变：仍由 `default_completion_verifier()` 判定。
- Skill 状态只驻留运行时内存与 Trace，不新增会话磁盘字段。

## 验证

- `tests/test_skills_*.py`：Manifest 严格性、路径穿越/符号链接/任意文件加载防护、越权拒绝、
  选择准确率与一致性、生命周期 Trace 完整率、`/skill off` 兼容、Host/Docker 兼容、指标聚合。
- 全量 `pytest`、`ruff check`、`ruff format --check`、`uv build` 通过。

## 非目标

不加载任意路径或第三方 Skill；不做 LLM 选择；不新增长期记忆/向量库/消息队列；不让 Skill 提权、
自动批准、更换沙箱或改变模型路由。
