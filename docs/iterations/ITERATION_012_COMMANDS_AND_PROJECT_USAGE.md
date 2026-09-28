# Iteration 012：具体模型选择、简明状态和项目用量

## 目标与基线

日期：2026-09-15。分支：`refactor/package-layout`。状态：`待用户审核`。
基线为 Iteration 011 的 425 项测试；本轮按照终端使用反馈完善四个本地命令。
没有修改 `.env`、本机 `pi-claw.toml`、模型角色分配、生成示例或既有会话用量记录。

## 修改清单

| 文件 | 修改 |
|---|---|
| `src/commands/__init__.py` | 移除 `/quit` 别名，仅保留 `/exit`；更新 help 用法与说明 |
| `src/commands/model.py` | 实际模型 ID 列表、角色映射、自动分工说明；唯一 ID 映射至已有配置，歧义拒绝 |
| `src/interfaces/terminal_input.py` | 模型补全候选携带实际名称和中文元数据，不请求模型列表 API |
| `src/interfaces/repl.py` | 装配动态模型菜单，切换入口解析实际 ID，成功提示显示实际模型 |
| `src/commands/status.py` | 默认五项简明中文信息；`--verbose` 保留原有 JSON 调试字段 |
| `src/commands/usage.py`（新增） | 当前会话与当前项目的输入/输出/总 Token、响应统计和记录缺口 |
| `src/commands/context.py` | 将用量显示拆到专用命令模块，保留上下文和历史操作 |
| `src/persistence/usage.py` | 工作区跨会话聚合；逐会话锁、归属检查、坏账本缺口、计数字段一致性校验 |
| `tests/test_command_dashboard.py`（新增） | 模型菜单/选择/歧义、简明/详细状态、聚合/隔离/损坏/并发、真实 CLI 管道 |
| `tests/test_commands.py`、`test_terminal_experience.py` | 更新唯一退出、模型补全和真实 PTY 状态输出断言 |
| README、配置/Runtime/工作流文档、迭代索引 | 命令用法、统计范围和兼容变化 |

新增和修改的接口保留中文说明。命令不进入 LLM 消息，不因查看状态或补全而消耗模型 Token。

## 使用与兼容性

退出旧进程后重新启动，更新才会生效：

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route primary --max-steps 24
```

在 `pi-claw>` 中使用：

```text
/model
/model deepseek-flash
/status
/status --verbose
/usage
/exit
```

`/exit` 成为唯一退出入口，`/quit` 将提示未知命令，不再退出。`/st` 仍不是完整命令，
输入 `/st` 后按 Tab 补齐 `/status`，再回车；不会擅自把任意命令前缀当完整命令执行。

`/model` 显示真实模型 ID 与配置别名，例如当前学校配置中的：

| 模型 ID | 配置别名 |
|---|---|
| qwen3.8-chat | economy |
| qwen3.7-plus | planner |
| deepseek-flash | primary |
| glm-5.3-flash | reviewer |
| qwen3.8-reasoner | reasoner |
| deepseek-v4-flash-ascend1 | alternative |

以上从本轮本机配置核对，不是自动探测服务可用性或新的模型能力评测。
自动模式仍保留 Iteration 011 的真实规划验证限制，本轮没有修复或改变该链路。

`/model deepseek-flash` 与当前配置下 `/model primary` 选择同一 profile，沿用其 API 连接、
thinking 和输出限制，不创建任意未知模型配置。实际 ID 有多个 profile 时，可能代表不同
连接或推理参数，必须选具体别名。已有别名优先匹配，`auto` 和 `scripted` 是保留模式名。
切换建立新会话，旧会话和原审批范围保留，不把旧历史静默发送给另一服务。
固定模型会跳过自动分类与独立规划/审查；`/model auto` 才使用已有自动分工。
这个 ID 解析是交互命令功能，启动参数 `--route` 仍使用 `auto` 或配置别名。

## status 字段说明与精简

默认输出只保留工作区、会话、模型、审批、最近结果。旧字段并非删掉数据，仍可在
`/status --verbose` 查看；Trace 和持久化协议未改变。

| 原字段 | 含义与本轮处理 |
|---|---|
| workspace | 模型工具可操作的工作区根路径；默认保留为“工作区” |
| session | 当前会话唯一 ID，用于恢复历史；默认保留为“会话” |
| provider | 接入方式：scripted 离线、openai 直接连接、routed 配置路由。routed 即使固定 primary 也成立；移至 verbose |
| model | 原值 primary 等是配置别名，auto 是模式，不是 API 模型 ID；默认改为显示实际 ID 或“自动路由” |
| messages | 活动上下文中的消息条数，包含 system、user、assistant、tool；新会话的 1 通常是 system，不是提问次数或 Token。移至 verbose，另可用 /context |
| auto_approve | 是否自动批准策略允许的副作用。false 不禁止写入，而是按需审批；会话已保存的授权可复用。默认以中文审批状态显示，仍受 Policy 限制 |
| last_status | 本次会话载入/切换后最近运行的宿主终态；null 表示尚无本进程结果，并不证明历史任务不存在。默认给出中文状态 |
| routing | 上次运行的路由、阶段、请求和用量详情；任务未运行时为 null。移至 verbose，避免默认输出大段内部 JSON |

## usage 的统计口径

1. 当前会话：该 Session 的所有已保存 run_id 的服务端 usage。
2. 当前项目累计：同一 `SessionStore.data_dir` 下，同一**工作区真实路径**的所有会话累计，
   已包含当前会话，不应把两行再次相加。
3. 输入 Token 包含每次请求实际发送的提示、历史和工具结果，重复发送也可能再次计数；
   输出 Token 来自服务端 completion_tokens。这里只求和，不通过字符数反推账单。
4. `/context` 展示活动窗口的近似大小，与历史请求累计 Token 不同；本命令也不查询账户余额。
5. 无 usage 响应、无记录会话和不可读账本分别显示。无记录可能尚未调用模型或历史版本没记录，
   不可读可能是损坏、符号链接或归属不符；均不当作实际零消耗。
6. 请求失败、运行中、退出前没来得及保存等可能产生未被本地统计的消费。已记录合计不是完整账单。

例如默认工作区是 `projects/`，则其中各任务共用这个统计范围。若希望 hello-world 单独计量，
使用 `--workspace projects/hello-world`；该工作区独立绑定会话，不递归合并到父工作区。
更换 `--data-dir` 也会改变账本来源。不会为旧消息补估 Token、扫描所有 Trace 拼凑数字，
或把其他显式工作区/测试存储的数据静默混入。

持久化仍是每个 Session 的 usage.json，以 run_id 幂等写入。项目合计按需逐会话读取，
没有另写容易重复累加的“项目总账”。每个会话独立加锁，跨会话不是全局原子快照；
查询期间其他进程新保存的数据可能下一次才看到。工作量随会话数和账本记录数增加。

## 验证

```bash
uv run --frozen pytest --cov=src --cov-report=term \
  --cov-report=json:runs/iteration-012-coverage.json --junitxml=runs/iteration-012-tests.xml
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv lock --check --offline
uv run --frozen pi-claw eval --workers 4 \
  --output runs/iteration-012-offline-eval.json --traces-dir runs/iteration-012-offline-traces
uv run --frozen pi-claw eval-routing --output runs/iteration-012-routing-eval.json
uv build --offline --out-dir runs/iteration-012-dist
git diff --check
git diff --cached --check
```

444 项测试通过，比基线新增 19 项，总体语句覆盖率 88%；模型命令 98%、状态命令 94%、
用量命令 100%、用量持久化 92%。真实 PTY 验证 `/` 菜单、Tab 补全和退出，真实 CLI 管道
验证查看模型、按 ID 切换、status、usage 和退出，使用无效测试域名且未发生模型请求。
工程 Eval 8/8，最大同时运行 4，隔离违规 0；路由合约 6/6。
Ruff、格式、编译、依赖锁检查通过。没有为本地界面功能额外调用真实模型。

测试覆盖账本幂等恢复、跨会话求和、其他工作区和其他存储目录排除、未知 usage、无记录会话、
坏 JSON、缺字段、负数、不一致计数、符号链接、归属不符，以及并发重复保存不重复计数。

## Git 与边界

- 修改保留在 `refactor/package-layout`，未新增暂存、commit、合并或 push。
- 保留此前未提交内容与原有 README 大小写重命名暂存。
- 仅移除 `/quit` 注册及其补全/退出行为；没有删除用户文件、会话或用量记录。
- 不改 API Key、URL、真实模型配置和上一轮的 hello-world 示例。
- 本地回归通过不是真实路由质量、模型能力或供应商计费准确性证明。
