# Pi-Claw

使用 Python 3.12 和 uv 的本地 Coding Agent。提供流式回复、受控工具执行、宿主完成验收、
任务锚与 pinned context、files / SQLite 可插拔会话存储，以及分层回归评测。

> **一句话定位**：把模型的工具调用放进可审计、可恢复、可验收的执行框架（Agent harness），
> 分别处理“模型看见什么”“状态如何保存”“操作是否允许”和“任务是否完成”。
>
> 文档核对：2026-10-16，代码基线 `bd75def`。M7b 仅完成精简短任务对照，
> 不代表长会话防漂移收益或跨机器分布式能力已经得到验证。

## 快速开始

```bash
uv sync --frozen
uv run --frozen pi-claw demo --yes
uv run --frozen pi-claw chat "检查当前示例工作区" --print
```

默认 scripted 模式完全离线，只验证工具和会话流程。demo 在临时工作区修改示例并运行测试。
实际仓库的写入和命令执行需要审批。

### 干净的默认工作区

从 Pi-Claw 根目录启动时，`chat` / `run` 默认使用 `projects/` 保存生成的代码示例和项目，
首次启动自动创建，不再默认修改 Pi-Claw 自身的 `src/`、`tests/` 或 README。
例如 `projects/hello-world/hello.py`。`projects/` 内的生成内容被 Git 忽略，仅跟踪 `.gitkeep`。
`.env`、模型 TOML 与 `runs/` 仍放在 Pi-Claw 根目录，不搬进生成项目。

显式操作一个已有项目时使用 `--workspace /ABS/PATH/project`；要修改 Pi-Claw 自身则明确用
`--workspace .`。默认路径是**启动目录下的 projects/**，因此推荐先 `cd` 到 Pi-Claw 根目录。
旧会话仍绑定原工作区；恢复以前根目录的会话需显式加 `--workspace .`，不会自动迁移或删除。

## 30 秒离线演示

`demo` 在临时工作区跑一个**确定性、完全离线**的修复流程（读到 bug → 改一处实现 → 跑测试），
不调用任何模型、不联网，退出即清理工作区：

```bash
uv run --frozen pi-claw demo --yes
```

真实输出（节选）：

```json
{
  "status": "completed",
  "steps": 4,
  "verification": {
    "accepted": true,
    "reason": "写入后满足必跑检查：test",
    "changed_files": ["calculator.py"]
  }
}
```

`verification.accepted` 只有在**写后真的跑通测试**时才为 `true`：完成声明不靠模型自述，
而由宿主用已经发生的工具证据验收（见“主要能力 · 完成验收”）。可用
`--trace runs/demo.jsonl` 导出这一步的完整轨迹，再 `pi-claw replay runs/demo.jsonl` 回放。

## 架构概览

```mermaid
flowchart TD
  CLI["CLI / REPL"] --> CMD["宿主命令 /goal /pin /context /usage"]
  CLI --> LOOP["Agent Loop / 顺序 Workflow / 只读 Team"]
  CLI <--> REPO["SessionRepository：事件 / v3 ActiveContext / Usage"]
  CMD <--> REPO
  REPO --> FILES["默认 files：journal + checkpoint + flock"]
  REPO --> SQL["可选 SQLite：WAL + 事务 + 双版本 CAS"]
  CLI --> ANCHOR["TaskAnchor：显式目标 + pins + 成功写入证据"]
  SKILL["声明式 Skill：默认 off / 不扩权"] --> CTX
  ANCHOR --> CTX["ContextManager：裁剪 / 压缩 / 请求前临时注入"]
  LOOP --> CTX
  CTX --> ROUT["模型路由 / 角色预算"]
  ROUT --> MODEL["Provider：scripted / OpenAI-compatible SSE"]
  MODEL --> TOOL["Tool Call → Hook / Policy / Approval / ToolExecutor"]
  TOOL --> EXEC["安全文件读写 / Host 或 Docker 命令运行器"]
  EXEC --> RESULT["ToolResult → 上下文 / Session / Trace"]
  RESULT --> LOOP
  LOOP --> VERIFY["CompletionVerifier：写后验证证据"]
  VERIFY --> OUT["RunResult / Usage / 脱敏 Trace"]
  EVAL["Eval harness：评分 → bad case 归因 → 有界修复 → 复测"] -.-> LOOP
```

一条任务的端到端数据流：

```text
用户任务
  → SessionRepository 保存事件与活动窗口，裁剪旧工具结果 / 必要时压缩
  → 支持的单 Agent 路径按请求临时组装 pins、任务锚和本回合 Skill 指导
  → Provider 流式返回 text_delta / tool_call_delta
  → before_tool Hook → Policy → Approval（需要批准则暂停，不静默放行）
  → ToolExecutor 执行（可选 Docker 断网隔离）→ ToolResult 写回上下文与原始日志
  → 重复调用检测；final 候选经过“完成验收”（写后必须有认可的成功验证证据）
  → RunResult 携带 usage → 幂等写入用量账本 + 可回放 Trace
```

图中是模块关系，不代表所有能力组合都已接通：pins/锚与 Skill 的已验证路径以单 Agent 为主，
Workflow / Team 的限制见下方“能力边界”。Trace 回放只复盘事件，不重新执行工具副作用。

### 关键取舍

| 选择 | 解决的问题 | 代价与边界 |
| --- | --- | --- |
| 手写 Agent harness，工具统一过宿主边界 | 可控制工具协议、预算、停止、恢复和验收 | 不提供通用图编排平台；模型自述不是成功证据 |
| 结构化 pins/锚，请求前临时物化 | 避免压缩吞掉约束、指导污染后续摘要 | 增加输入 Token；超预算明确失败，不保证模型遵守 |
| 确定性压缩与锚构建 | 零额外摘要模型请求，便于离线复现 | 历史细节有损；不是语义检索或跨会话记忆 |
| files 默认，SQLite 可选 | 保留本地 CLI 的低依赖，同时提供事务与迁移 | files 多文件不是整体事务；SQLite 同库写入串行，不是分布式存储 |
| 日志序号与快照修订号分开 | 追加消息与“不追加消息的 pin/压缩更新”分别检测冲突 | 调用方必须传入观察到的版本；CAS 不替代权限校验 |
| 子 Agent 只读，主 Agent 唯一写者 | 减少并发写冲突，支持并行定位与独立复核 | 增加 Token、延迟与交接成本，并非所有任务都更合算 |
| Host 默认、Docker 显式启用且失败不降级 | 兼容本地工具，同时提供更强的命令隔离 | Host 不是 OS 沙箱；Docker 仍共享内核 |

工程约束：

- **依赖方向单向**：`interfaces → core → tools/security/persistence`；Runtime 不导入
  `evaluation`，评测按需加载，评测逻辑不进入产品路径。
- **唯一工具执行入口**：所有模型工具都经过 `ToolExecutor`，统一走 Policy/Approval 与路径边界，
  没有绕过审批的旁路。
- **完成验收不信任模型自述**：`ToolEvidenceCompletionVerifier` 要求“成功写入后出现受认可的成功
  验证命令”；宿主声明的 `VerificationPlan` 还能保护测试文件、拒绝无关测试绿灯。
- **可回放**：一次运行落 `journal` + `checkpoint` + 脱敏 `trace`，可复盘每一步工具调用与结果。
- **三套评测彼此独立**：内部冻结回归、内部分布外 holdout、外部 SWE-bench Verified 互不复用，
  避免“用自己出的题自证”。

## 指标与证据边界

以下真实模型/攻击数字引用历史验收记录，不是本次文档更新重新实跑的成绩；不同任务集、预算和
修复口径不能混成一个总分。任务名称、筛选条件见 [任务集说明](docs/EVALUATION_TASKSETS.md)。

| 评测 | 结果 | 必须保留的口径 / 证据 |
| --- | --- | --- |
| 内部冻结基线 | **78/90**，单 trial 420s | 10 任务 × 3 架构 × 3 次；冻结基线不被后续成绩覆盖 |
| 后续诊断、续跑和修复派生结果 | **90/90**，各模式 30/30 | 使用放宽至 1800s 的诊断预算并叠加修复/端点复测，不是原 420s 首次通过率；[Iteration 038](docs/iterations/ITERATION_038_STAGE3_FULL_REGRESSION.md) |
| 独立 holdout | **27/27**，pass@3、pass³ 均 1.0 | 3 新任务 × 3 架构 × 3 次，420s；[Iteration 039](docs/iterations/ITERATION_039_STAGE3_HOLDOUT.md) |
| SWE-bench Verified 健康子集 | 首轮 **20/37**；≤2 次修复后 relative **37/37** | 本地判定的 official_resolved 为 **24/37**，不是官方榜单成绩；[Iteration 040](docs/iterations/ITERATION_040_SWEBENCH_VERIFIED_EXTERNAL_EVAL.md) |
| 沙箱专项 | **11/11** 攻击阻断；连续运行完成/清理 **30/30** | Host/Docker 确定性 parity；只覆盖列举攻击，不是安全证明；[Iteration 026](docs/iterations/ITERATION_026_SANDBOX_ATTACK_EVAL.md) |
| pinned context 离线机制 | 连续 **20 次压缩**保留约束；锚构建额外模型请求 **0** | 固定语料机制验证，不等于模型漂移率为 0；[M1](docs/acceptance/session-context/M1_PINNED_CONTEXT.md)、[M7a](docs/acceptance/session-context/M7A_CONTEXT_RELEASE.md) |
| 任务锚真实模型精简对照 | **16/16**，四组各 4/4、约束零违反 | 2 短任务 × on/off × files/sqlite × 2 次；两臂都成功，**不能证明降低漂移**；[M7b](docs/acceptance/session-context/M7B_EVALUATION_AND_RELEASE.md) |

成本同样需要报告：holdout 中 single / workflow / team 的每成功任务 Token 分别为
**12,203.67 / 54,918.00 / 42,875.78**；三者均 9/9，但多角色不是免费收益。
锚精简对照中，on 相对 off 平均输入/输出合计 Token 增加约 **1.5%（files）/ 2.9%（sqlite）**；
样本小、未覆盖长会话，不能把耗时差异归因于锚或 SQLite 的性能提升。

bad case 闭环示例：外部首轮 17 个失败中 13 个为重复只读调用。依据 Trace 增加软循环恢复与
写优先提示后，在这 17 例重测中 9 例通过、循环事件 13→0。**29/37 是沿用原 20 个成功结果，
再合入这 9 个成功的重构数字**，不是新版本全量独立重跑的 37 例首轮成绩。筛选限定轻量 Python
实例，本地 aarch64 环境使用同环境 gold 基线；relative 37/37 不外推完整 Verified。

2026-10-16 对 `bd75def` 的当前本地验证：解除执行环境限制后，
`python -m pytest -o addopts='' -q` 为 **1028 passed**；Ruff、187 个文件格式检查和离线构建通过。
受限环境同次复核为 `1021 passed, 4 failed, 3 skipped`，因此测试结果须同时说明环境。
本次未重新调用模型评测；不能用上述工程测试数量代替真实任务成功率。

## 会话上下文：结构化状态与可插拔存储

`ActiveContext v3` 统一保存普通消息、`goal`、`pins`、`anchor`、`through_seq` 和
`snapshot_revision`。`/goal` 显式设定当前目标，`/pin` 固定用户约束；任务锚从目标、约束和成功
写入证据确定性构建，最多 2,000 字符，不额外调用模型。pins/锚/Skill 指导在请求前临时物化，
不把合成消息写回普通历史，防止深拷贝或压缩后残留。它们依旧受宿主权限与总上下文预算限制。

`SessionRepository` 隔离存储介质：默认 files 用 journal、原子 checkpoint 和 `flock`；SQLite 用
WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 事务保存事件与快照。`expected_seq` 检测日志竞争，
`expected_revision` 检测同一日志水位下的快照竞争；Usage 按 `run_id` 幂等记账。已有多进程竞争、
事务中 kill、独立连接直读和双向迁移测试，见 [M4](docs/acceptance/session-context/M4_CONCURRENCY_AND_RECOVERY.md)、
[M5](docs/acceptance/session-context/M5_MIGRATION.md)、[M6b](docs/acceptance/session-context/M6B_BACKEND_COMMANDS.md)。

### 能力边界

- 任务锚、pins 与 Skill 主要在单 Agent 路径验证；已识别的 Workflow 会警告不注入 pins/锚，
  Team 未提供共享锚接线，不宣称三模式全覆盖。
- 当前目标由 `/goal` 显式设置，不会自动识别所有自然语言目标变更。锚构建器支持验证指纹失效判断，
  但 REPL 当前尚未把最近验证结果/工作区指纹传入，不能宣称这部分已经端到端闭环。
- 默认锚开关开启；`PI_CLAW_TASK_ANCHOR=off` 只关闭回合内锚注入，不删除已保存状态，不关闭 pins
  或安全检查。Skill 默认关闭；无自动技能生成、任意脚本插件或跨会话语义记忆。
- Skill 激活只检查声明所需工具属于现有 Policy 允许集合，再注入指导；不会裁减 Agent 的工具集合。
  “只读定位”是该 Skill 的流程指导，不等同于宿主强制禁写，硬权限仍由 Policy/Approval 控制。
- SQLite 是单机嵌入式数据库，不是多机分布式存储；进程 kill 测试不等于断电耐久性证明。
  files↔sqlite 为一次性迁移，非双向同步；迁移前停写，切换后不自动合并两边的新数据。
- Host 后端不是 OS 沙箱；Docker 只隔离命令运行，不能替代文件工具权限检查或保证容器绝不逃逸。
- 完成验收只证明配置要求的证据成立，不保证业务语义完全正确；Trace 回放不是副作用重放。

## 连接模型

项目根目录提供 `.env.example`，本机填写 `.env`（Git 已忽略；Finder 中按
`Command + Shift + .` 显示隐藏文件）。从项目目录启动时自动读取 `.env`，终端中已导出的
同名变量优先。可以在 `.env` 中填写当前供应商配套的 API Key、Base URL 和准确模型 ID：

```dotenv
OPENAI_API_KEY=<YOUR_API_KEY>
OPENAI_BASE_URL=<YOUR_CHAT_COMPLETIONS_BASE_URL>
OPENAI_MODEL=<YOUR_MODEL_ID>
```

```bash
uv run --frozen pi-claw chat --provider openai
```

适配 OpenAI-compatible Chat Completions，可配置 Kimi、Qwen、DeepSeek 等服务。
供应商获取入口和配置步骤见 [API Key 指南](docs/API_KEY_GUIDE.md)。
真实服务兼容性和小示例联调记录见 [最新迭代](docs/iterations/ITERATION_011_WORKSPACE_TERMINAL.md)，
离线测试通过不等于所有模型和任务都可用。

chat 默认请求 SSE 流；供应商不支持时使用 `--no-stream`。使用 `--max-attempts 3`
设置请求最多尝试次数。终端 `candidate(stream)>` 表示尚未验收的实时文本。
流式响应中断后，尚未形成完整模型消息的候选会被明确标记作废，并在有界次数内重试；
只有完整工具调用才可能进入审批与执行。单模型入口的读超时默认 120 秒，可用
`--timeout` 或 `.env` 中的 `OPENAI_TIMEOUT_SECONDS` 调整。
若正式回复与流式正文完全一致，只打印简短完成状态，不再重复整段 `assistant>`；
工作流只追加新的审查结论。同步回复、修订结果、未完成状态仍会显示，完整消息照常保存。

真实模型与默认交互链路需显式冒烟验证；该测试在临时工作区执行纯问答、`/status`、
`/usage` 和 `/exit`，并检查没有多余工具调用。普通 CI 不发起真实模型请求：

```bash
PI_CLAW_LIVE_CHAT=1 uv run --frozen pytest -m live_model tests/test_live_interactive.py
```

## 按复杂度选择模型

复制 `pi-claw.example.toml` 为 `pi-claw.toml`。同一服务/网关下的多个模型共用 `.env` 中的
`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`，各 profile 只需填写不同的准确模型 ID，不需要价格：

```toml
[connection]
base_url_env = "OPENAI_BASE_URL"
api_key_env = "OPENAI_API_KEY"

[routing]
intent_strategy = "model"
cheap = "economy"
strong = "primary"

[models.economy]
model = "<YOUR_LOW_COST_TOOL_MODEL_ID>"

[models.primary]
model = "<YOUR_PRIMARY_TOOL_MODEL_ID>"
```

路由时复用 API 地址和 Key，改变请求 JSON 中的 `model`；不会在本机加载模型权重。
路由模式不使用单模型入口的 `OPENAI_MODEL`。若某个 profile 连接另一家服务，须同时显式
配置它的 `base_url`/`base_url_env` 和 `api_key_env`，不能继承半套共享连接。通过显式配置启用路由：

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route auto
uv run --frozen pi-claw chat --config pi-claw.toml --route primary
uv run --frozen pi-claw eval-routing --output runs/routing-eval.json
```

每回合先让轻量模型只判断 `simple/complex`：简单意图识别、分支选择、短文本提取和单步只读
任务由轻量模型执行；代码修改、调试、架构设计、多步骤任务由主力模型执行。
分类失败或含糊时转主力，明显副作用另有宿主规则兜底；工具失败、轻量执行配额耗尽或越界
工具候选也会升级。模型角色由你指定，不根据未知价格排序。
分类无工具权限、不进入任务历史，但计入总请求和 Token；每回合最多分类一次。
`/model` 查看具体模型或切换，`/status` 查看简明状态，`/status --verbose` 查看分流详情，
`/usage` 查看会话与项目累计 Token。旧配置省略 `intent_strategy` 时仍用本地规则。
请求次数、输出 Token 上限和复现实验见
[配置与模型路由](docs/CONFIGURATION_AND_ROUTING.md)。

## 自动规划、编码与审查

`pi-claw.ustc.example.toml` 中分类、规划、编码和常规审查都使用学校免费的
`deepseek-flash`；只有命中确定性高风险规则时，才通过独立 Moonshot 连接调用用户自费
`kimi-k3` 做第二次只读审查。新克隆可复制为本机 `pi-claw.toml`；已经配置好的文件不要覆盖。
学校连接使用 `.env` 的 `OPENAI_*`，Kimi 使用独立的 `MOONSHOT_*`：

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route auto --max-steps 24
```

简单问答保留轻量路径，复杂任务进入完整工作流；审查不通过会带问题清单返回编码。
规划和审查被宿主限制为只读，写入与命令仍需审批。审查必须重读改动文件，宿主仍检查写后
验证证据。显式要求、高风险能力、两轮以上常规审查或至少五个改动文件才升级 Kimi；没有成功
改动文件时不调用。Kimi 每个逻辑请求只尝试一次，否决或失败不会伪装成通过。
学校模板最多三轮常规审查、一次高级审查阶段、全回合 36 次逻辑请求，不需要模型价格。
这不是并行子代理，也不自动提交或回滚。准确配置、数据流、预算、恢复边界及待实测项见
[多模型工作流](docs/WORKFLOW_PIPELINE.md)。

## 主要能力

| 能力 | 行为 |
|---|---|
| 工具执行 | 文件读取、搜索、精确修改和白名单 argv 命令，统一经过 Policy/Approval |
| 流式事件 | 文本/工具参数 delta、请求重试、工具结果、上下文变化和运行终态 |
| Token 统计 | 累计服务端 usage；通过 `/usage` 查询当前会话与工作区历史累计，缺口显式提示 |
| 上下文管理 | 确定性压缩、工具正文裁剪、结构化 pins/任务锚与请求前临时注入 |
| 请求重试 | 暂时性 HTTP/网络错误有限退避；未完成的流式候选作废后可重试，完整消息和工具不重放 |
| 重复检测 | 近期相同参数与结果重复出现时停止无进展循环 |
| Hooks | 显式注入生命周期回调；前置工具 Hook 可拒绝，不能绕过审批 |
| 会话恢复 | files / SQLite 统一仓储、v3 快照、双版本冲突检查、双向一次性迁移 |
| 完成验收 | 修改后缺少认可验证证据时拒绝完成声明，返回 unverified 或继续验证 |
| 离线评测 | 真实工具链、独立工作区并发、机器可读报告和脱敏 Trace |
| 模型路由 | 轻量模型分类、按复杂度分流、失败升级、固定 profile、分模型 Token 与请求上限 |
| 多模型工作流 | DeepSeek 规划/编码/常规审查、按风险升级 Kimi、高级只读隔离和结构化产物持久化 |
| 声明式 Skill | 内置任务执行指导（只读定位、测试修复）；确定性选择、默认关闭、不提权、不执行代码 |
| Docker 命令隔离 | 去敏只读快照、断网非 root 容器、资源硬限制、强制清理和 11 类攻击回归 |

完成验收是最低证据检查，不保证任务业务正确。默认 Host 后端不提供 OS 沙箱；显式 Docker 后端
显著加强命令隔离，但容器仍不是虚拟机。详细边界见 [安全说明](docs/SECURITY.md)。

构建本地固定沙箱镜像并运行确定性安全评测：

```bash
./scripts/build-sandbox-image.sh
uv run --frozen pi-claw chat --config pi-claw.toml --route auto --sandbox docker
uv run --frozen python -m evaluation.sandbox_cases \
  --output runs/stage3-sandbox-eval.json --trials 30
```

评测报告只保存聚合安全/性能指标，不保存 argv、stdout、宿主路径、canary、API Key 或 Base URL。

## 会话与命令

```bash
uv run --frozen pi-claw chat --continue
uv run --frozen pi-claw chat --session <SESSION_ID>
uv run --frozen pi-claw sessions
uv run --frozen pi-claw eval --workers 4
uv run --frozen pi-claw replay <TRACE_PATH>
```

REPL：`/help`、`/status [--verbose]`、`/model [模型ID|auto|别名]`、`/new`、`/sessions`、`/resume <id>`、`/compact`、`/context`、
`/pin [约束|-d <id>]`、`/goal [目标|reset]`、`/usage`、`/history`、`/title <标题>`、`/exit`。
`/skills` 列出内置 Skill；`/skill auto|<id>|off` 启用自动选择、显式选择或关闭。

在 `pi-claw>` 提示符下输入 `/` 即显示命令菜单，继续输入可筛选；Tab / ↑↓ 选择，回车执行。
`/model ` 补全具体模型 ID 和带模型说明的配置别名，例如 `/model deepseek-flash`；同一 ID
对应多个配置时需用别名。`/exit` 是唯一退出命令，原 `/quit` 别名已移除。
普通终端管道或 `TERM=dumb` 不显示弹出菜单，但 `/` 回车仍可
查看帮助，`/status`、`/exit` 回车仍正常执行。命令补全与帮助不会调用模型，输入历史仅驻留内存。
这些不是 zsh 的系统命令。更新后需退出并重新启动正在运行的 Pi-Claw。

`/status` 默认只显示工作区、会话 ID、具体模型/模式、审批和最近结果。消息数等调试字段移到
`/status --verbose`，其中 `messages` 包含系统和工具消息，不是用户提问次数或 Token。
`/usage` 显示输入、输出、总 Token 及统计缺口；项目按工作区真实路径划分，汇总同一会话存储下
该工作区所有会话，已包含当前会话，不能把两行再次相加。更换 `--workspace` 或 `--data-dir`
会改变统计范围。若要逐个子项目独立计量，启动时指定 `--workspace projects/子项目`。
这些是已持久化的服务端统计，不是账户余额、完整账单或 `/context` 的上下文 Token 估算。
字段解释与验证记录见 [Iteration 012](docs/iterations/ITERATION_012_COMMANDS_AND_PROJECT_USAGE.md)。

会话默认保存在 `~/.pi-claw/sessions/`，可用 `PI_CLAW_HOME` 或 `--data-dir` 调整。
会话后端默认 `files`；设 `PI_CLAW_SESSION_BACKEND=sqlite` 或传 `--session-backend sqlite` 可改用
数据目录下单个 `sessions.sqlite3`（CLI 参数优先于环境变量）。已有会话用 `pi-claw migrate
--workspace <目录> --from files --to sqlite --data-dir <数据目录>` 一次性迁移，源数据保留、
重复运行幂等、冲突在写入前报错；`/context` 会显示当前实际后端。
Trace 和 Eval 报告默认保存在忽略跟踪的 `runs/` 中。

上下文与 SQLite 的离线交互演示（先启动，后在 `pi-claw>` 中输入命令）：

```bash
uv run --frozen pi-claw chat --provider scripted --session-backend sqlite
```

```text
/goal 修复当前项目，保持公共接口兼容
/pin 不修改测试文件
/context
/compact
/context
/pin -d p1
/goal reset
/exit
```

`/goal reset` 重置目标但保留 pins；`/context` 展示实际后端、水位线、修订号与锚构成。
这段演示验证状态与命令，不调用真实模型，也不证明模型遵守了约束。

## 目录

```text
pi-claw/                  # Git/uv 项目根目录
├── README.MD             # 项目入口说明
├── .env.example          # 可提交的空密钥模板
├── .env                  # 本机填写，Git 忽略
├── pi-claw.example.toml  # 模型 profile/路由示例
├── pyproject.toml
├── uv.lock
├── src/
│   ├── tools/            # read/write/edit/grep/shell、注册、统一审批执行
│   ├── commands/         # status/model/exit/sessions/help、上下文命令
│   ├── config/           # 配置类型、.env 解析、TOML 加载
│   ├── routing/          # 复杂度分类、模型路由、顺序工作流、用量账本
│   ├── core/             # Agent、ActiveContext、任务锚、压缩、Hooks、完成验收
│   ├── skills/           # 声明式 Manifest、确定性选择与单回合指导
│   ├── security/         # Policy、Approval、Workspace
│   ├── sandbox/          # Host / Docker 命令运行与清理
│   ├── providers/        # 离线 Provider、HTTP/SSE
│   ├── persistence/      # SessionRepository、files/SQLite、迁移、Usage、Trace
│   ├── evaluation/       # 正式 Runtime 离线验收与路由对照
│   └── interfaces/       # CLI、REPL
├── external_eval/        # 独立外部评测（SWE-bench Verified）；不进入主 pytest/CI
├── tests/
├── docs/
├── projects/             # 默认示例/代码项目工作区；生成内容不进入 Pi-Claw Git
├── runs/                 # 本地 Trace 和评测报告，内容不进入 Git
└── scripts/              # 镜像构建、评测进度看板等运维脚本
```

业务模块直接位于 `src/core/`、`src/interfaces/` 等目录，不再嵌套 `pi_claw/`。
Python 导入使用 `from core.agent import Agent`；常用启动命令仍为 `uv run --frozen pi-claw`，
模块启动方式改为 `uv run --frozen python -m interfaces`。

## 开发验证

变更批次的审核、提交、推送和稳定分支集成规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen pytest
uv run --frozen pytest -m docker_integration tests/test_docker_sandbox_integration.py
uv run --frozen pi-claw eval --workers 4
uv build --no-config
```

`--frozen` 按现有锁文件运行，避免本机包索引配置导致锁文件地址变化。

## 文档

- [架构与依赖方向](docs/ARCHITECTURE.md)
- [工作流程与数据路径](docs/WORKFLOW_AND_DATA_FLOW.md)
- [运行时能力与 Hooks 接口](docs/RUNTIME.md)
- [配置与模型路由](docs/CONFIGURATION_AND_ROUTING.md)
- [安全与授权边界](docs/SECURITY.md)
- [评测指标](docs/EVALUATION.md)
- [评测任务名称、选择理由与限制](docs/EVALUATION_TASKSETS.md)
- [上下文第一批机制验收](docs/acceptance/session-context/M7A_CONTEXT_RELEASE.md)
- [SQLite 接线与独立落库验证](docs/acceptance/session-context/M6B_BACKEND_COMMANDS.md)
- [任务锚精简真实模型对照](docs/acceptance/session-context/M7B_EVALUATION_AND_RELEASE.md)
- [单 Agent、顺序工作流与主从多 Agent 对照](docs/AGENT_MODE_COMPARISON.md)
- [外部评测：SWE-bench Verified 与坏例自修复](docs/iterations/ITERATION_040_SWEBENCH_VERIFIED_EXTERNAL_EVAL.md)
- [声明式 Skill：边界、Manifest 与命令](docs/SKILLS.md)
- [项目范围](docs/PROJECT_SCOPE.md)
- [变更与集成流程](CONTRIBUTING.md)
- [迭代记录](docs/iterations/README.md)

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。

本轮目录和运行时边界参考 [pichu](https://github.com/yeabwang/pichu) 的模块划分，
基于原有 Pi-Claw 实现独立演进，取舍与能力对照见迭代记录。
