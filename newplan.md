# Pi-Claw Skill MVP 实施计划

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

## 阶段计划

### P0 骨架与 Manifest 校验
- 新增 `src/skills/__init__.py`、`errors.py`、`models.py`、`loader.py`。
- 严格 Manifest：`id, version, description, task_types, trigger_keywords, required_tools,
  read_only, max_prompt_chars, prompt_file`；拒绝未知字段/错类型/重复 id/坏版本。
- 常量：`SKILL_ID_RE`、`SEMVER_RE`、`PROMPT_FILE_ALLOWED`、硬上限（文件 ≤ 8 KiB、prompt ≤ 8000 字符）。
- 路径防护：仅限内置目录、无 `..`、无绝对路径、无符号链接；`prompt_file` 白名单。
- 测试：`tests/test_skills_models.py`、`tests/test_skills_loader.py`。

### P1 注册表与确定性选择
- 新增 `src/skills/registry.py`、`selector.py`。
- 内置发现 + id/version/文件名白名单 + 去重；`list()` 稳定排序。
- 选择优先级实现 + 阈值 + 并列即不选；负例（无关/弱命中）不激活。
- 测试：`tests/test_skills_registry.py`、`tests/test_skills_selector.py`（准确率 100%、一致性 100%）。

### P2 执行、Trace 与 Agent 注入
- 新增 `src/skills/executor.py`：工具子集校验、guidance 生成、生命周期 Trace。
- 修改 `src/core/agent.py`：`run()/_run()` 增加 `skill_guidance`；注入 + `finally` 剥离。
- 测试：`tests/test_skills_executor.py`、`tests/test_skills_security.py`（越权 0、穿越 0、Trace 完整 100%）。

### P3 终端接口
- 新增 `src/commands/skills.py`：`/skills`、`/skill auto|<id>|off`（只接受 id，不接受路径）。
- 修改 `src/commands/__init__.py`（注册）、`src/commands/status.py`（显示当前 Skill/方式/版本）、
  `src/interfaces/repl.py`（构建 registry、run_turn 接线、保存运行时状态）。
- 测试：`tests/test_skills_commands.py`。

### P4 内置 Skill 资源与打包
- 新增 `src/skills/builtin/repository-investigation/{skill.toml,prompt.md}`（只读定位）。
- 新增 `src/skills/builtin/python-test-repair/{skill.toml,prompt.md}`（复现→定位→最小改→专项→回归；
  写权限仍由 Policy/Approval 决定）。
- 修改 `pyproject.toml`：hatch `packages` 增 `src/skills`，确保资源随 wheel 打包；重装 editable。
- 测试：`tests/test_skills_builtin.py`（两 Skill 合法、只读约束、prompt 上限）。

### P5 集成与量化
- `tests/test_skills_integration.py`：`/skill off` 与基线行为一致；Host/Docker 兼容；
  端到端一轮注入与剥离。
- 量化（复用现有 `src/evaluation`，不新增评分器）：Skill off vs on 的严格成功率、首次成功率、
  总 Token、每成功 Token、端到端耗时、工具重复调用数、权限拒绝/越权次数；产出对照报告（不入库）。

### P6 文档与收口
- 新增 `docs/SKILLS.md`；更新 `README.MD`、`docs/PROJECT_SCOPE.md`、`docs/ARCHITECTURE.md`；
  新增迭代文档 `docs/iterations/ITERATION_041_SKILLS_MVP.md`。
- 全量 `pytest` / `ruff check` / `ruff format --check` / `uv build --no-config`。

---

## 验收清单（对齐需求）
- 固定选择用例准确率 100%；负向误激活 0；同输入重复选择一致率 100%。
- 越权成功 0；路径穿越 / 任意文件加载成功 0；Trace 生命周期完整率 100%。
- Skill 关闭后与现有 Agent 行为兼容；Host / Docker 行为兼容。
- Manifest 严格字段校验；未知字段/重复 ID/非法工具名拒绝；无 Key/URL 凭据。
- 全量 pytest、ruff、format、build 通过。

## 进度
- [ ] P0 骨架与 Manifest 校验
- [ ] P1 注册表与确定性选择
- [ ] P2 执行、Trace 与 Agent 注入
- [ ] P3 终端接口
- [ ] P4 内置 Skill 资源与打包
- [ ] P5 集成与量化
- [ ] P6 文档与收口
- [ ] 全部完成后停止，等待用户审核并合并 `main`
