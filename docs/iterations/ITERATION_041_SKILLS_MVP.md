# Iteration 041：声明式 Skill MVP

## 状态与边界

状态：实现与离线验证已完成；等待推送 `optimize` 与用户审核后合并 `main`。

本轮新增**声明式 Skill MVP**：用结构化 Manifest + 提示模板，提升 Agent 对典型开发任务的执行
稳定性，同时**复用**现有上下文、持久化、审计、用量、权限与工作流组件。明确不做：新增长期记忆、
Redis、向量库或任意代码加载；不重复实现会话持久化；不新增 LLM 选择；不改评分器。

默认关闭（`/skill auto|<id>` 显式启用），仅在单 Agent 路径（`provider.workflow is None`）激活。

## 目标

- Skill 只是“受限的任务执行指导”，不扩大权限、不自动批准、不更换 Sandbox、不改变模型路由。
- 选择完全本地、确定性、零 LLM。
- 完成后仍由现有 `CompletionVerifier` 与真实测试验收。

## 实现

| 文件 | 修改 |
|---|---|
| `src/skills/models.py` | `SkillManifest`/`Skill`/`SelectionMethod`/`SkillSelection`；严格字段与取值校验 |
| `src/skills/errors.py` | `SkillError` 及 `ManifestError`/`PathViolation`/`PromptTooLarge`/`DuplicateSkill`/`SecretDetected`/`ToolNotAllowed`/`SkillNotFound` |
| `src/skills/loader.py` | 解析 `skill.toml`、路径/符号链接防护、文件与字符上限、秘密扫描 |
| `src/skills/registry.py` | 拒绝重复 ID、稳定排序、内置发现；工具白名单取自真实注册表 |
| `src/skills/selector.py` | 固定优先级：显式 > 任务类型 > 关键词评分 > 不选择 |
| `src/skills/executor.py` | 工具子集校验、受限 guidance 渲染、生命周期 Trace |
| `src/skills/metrics.py` | 只读聚合：事件计数与生命周期完整率 |
| `src/skills/builtin/repository-investigation/` | 只读定位 Skill（Manifest + 提示） |
| `src/skills/builtin/python-test-repair/` | 测试修复 Skill（复现→定位→最小改→专项→回归） |
| `src/core/agent.py` | `run(..., skill_guidance=..., run_id=...)`：单回合受限注入 + 结束剥离 |
| `src/interfaces/repl.py` | 构建注册表/选择器/执行器、`run_turn` 接线、Skill 状态 |
| `src/commands/skills.py`、`__init__.py` | `/skills`、`/skill auto|<id>|off` |
| `src/commands/status.py` | 显示当前 Skill、选择方式与版本 |
| `src/tools/registry.py` | 新增 `known_tool_names()` 供 Manifest 校验 |
| `pyproject.toml` | hatch `packages` 增加 `src/skills`；资源随 wheel 打包 |

## 设计取舍

- **默认关闭**：保证“关闭即与现有行为一致”，也便于 Skill on/off 对照。
- **单回合注入并剥离**：指导作为受限 system 消息插在系统提示之后、用户任务之前；`run()` 的
  `finally` 按对象身份移除，避免污染活动窗口或后续回合。
- **工具子集校验前移**：`read_only` 不得声明写/命令工具在加载期即拒绝；运行时再校验所需工具是
  当前策略允许集合的子集，越权记录 `skill_rejected` 并拒绝激活。
- **零 LLM 选择**：确定性匹配，同输入必得同结果，不增加 Token 与延迟。
- **只记元数据**：Trace 事件只含标识与选择方式，绝不写完整提示或工作区正文。
- **只接受内置 ID**：命令层拒绝任何路径形式，杜绝任意文件加载。

## 验证

```text
外部评测未受影响；Skill 相关离线测试：
  tests/test_skills_models.py / loader.py / registry.py / selector.py / executor.py
  tests/test_skills_agent_injection.py / commands.py / builtin.py / metrics.py / integration.py
  合计 72 passed, 1 skipped（Docker 用例默认按标记跳过；沙箱外运行通过）
固定选择用例准确率 100%；负向误激活 0；重复选择一致率 100%
越权成功 0；路径穿越/任意文件加载成功 0；Trace 生命周期完整率 100%
`/skill off` 与基线消息序列一致；Host 与 Docker 后端均完成只读 Skill 生命周期
wheel 含 skills 包与 builtin 资源
```

全量 `pytest` 仅剩 4 项与本次改动无关的既有失败（`test_sandbox_lifecycle` 2、
`test_terminal_experience` 2）；`ruff check`、`ruff format --check`、`uv build` 通过。

## 风险与遗留

- 关键词匹配为字面量匹配，跨语言/近义表达可能漏配；命中不明确时选择不激活，风险受控。
- 描述性能提升需要真实模型 A/B（Skill on/off 的严格成功率、首次成功率、Token、耗时、工具重复
  调用、权限拒绝/越权）；本轮离线只验证机制与指标聚合，真实模型对照留待后续。
- 第一版仅内置两个代表 Skill；不做第三方/任意路径加载。
