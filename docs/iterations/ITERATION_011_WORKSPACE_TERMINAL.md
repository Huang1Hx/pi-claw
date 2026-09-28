# Iteration 011：干净工作区、斜杠菜单和单次输出

## 目标与基线

日期：2026-09-15。分支：`refactor/package-layout`。状态：`待用户审核`。
基线为 Iteration 010 的 393 项离线测试。本轮根据用户要求：

1. 在 Pi-Claw 根目录建立专门保存生成代码的干净工作区，并作为终端默认目标。
2. 输入 `/` 时提供可发现、可选择的本地命令，完善 `/status`、`/exit` 等使用体验。
3. 同一回复不再以 candidate(stream) 与 assistant 完整重复显示。
4. 用户授权调用学校模型做受限测试；不自动提交、合并或推送 Git。

## 文件和行为修改

| 文件 | 修改内容 |
|---|---|
| `projects/.gitkeep`、`.gitignore` | 新默认工作区，只跟踪空目录占位；生成项目不进入 Pi-Claw Git |
| `src/config/workspace.py`（新增） | 默认使用启动目录/projects，拒绝隐式符号链接和同名普通文件；显式目录优先，sessions 只读不创建 |
| `src/interfaces/cli.py` | chat/run/sessions 共用新工作区解析，不改变 .env 来源；更新帮助和执行风险提示 |
| `src/interfaces/terminal_input.py`（新增） | prompt-toolkit 交互菜单、命令和模型角色补全、单候选同步 Tab、非 TTY 降级、仅内存历史 |
| `src/commands/registry.py` | `/` 回车显示帮助，不完整命令提示可补全名称，不进入 LLM |
| `src/interfaces/rendering.py`（新增） | 有界记录已展示候选，精确正文去重、仅追加新审查结论、隐藏纯空白标签、保留修订/失败/同步结果 |
| `src/interfaces/repl.py` | 装配输入和显示层，添加菜单操作提示，每回合重置显示状态，批准审查摘要不重复打印 |
| `src/routing/classifier.py` | 提示和宿主规则覆盖“写一个、新建、创建、保存”等代码落盘需求，避免短示例误走只读问答 |
| `src/core/prompts.py` | 默认代码新建请求需通过工具保存到当前 workspace；明确只展示的请求例外 |
| `pyproject.toml`、`uv.lock` | uv 管理 prompt-toolkit 3.x；本次锁定 prompt-toolkit 3.0.53、wcwidth 0.8.3 |
| `tests/test_terminal_experience.py`（新增） | 默认/显式工作区、真实文件写入与会话隔离、补全、真实 PTY、去重与原文持久化 |
| `tests/test_model_intent_routing.py` | 新增 7 项短代码生成/保存请求宿主兜底回归 |
| `tests/test_workflow_transport.py` | 两种真实 CLI mock 模式断言正文和审查摘要各只显示一次 |
| README、配置/Runtime/架构/范围/工作流文档 | 新工作区路径、旧会话恢复、交互方式、输出规则和真实测试边界 |

新增源文件、类、函数和关键边界保留中文注释。没有改动真实 Key、URL 或持久化格式。

## 使用方式与兼容性

```bash
cd /ABS/PATH/pi-claw
uv sync --frozen
uv run --frozen pi-claw chat --config pi-claw.toml --route auto --max-steps 24
```

启动后 workspace 应为 `/ABS/PATH/pi-claw/projects`，不再是 Pi-Claw 源码根目录。
可在其中建立 `hello-world/` 等独立项目子目录。默认目录相对**启动目录**，并非硬编码个人路径；
其他已有项目使用 `--workspace /ABS/PATH/project`。不会自动迁移或删除旧文件。
`.env`、相对 `--config`、Trace 仍按启动目录解析；模型工具不能访问工作区父目录。

旧会话仍绑定原工作区。若需要恢复以前直接使用 Pi-Claw 根目录的会话，显式加
`--workspace . --session <旧ID>`。默认目录的会话列表与旧目录互不混用，审批也不迁移。

在 `pi-claw>` 输入 `/` 即弹出命令名称和中文说明，继续输入筛选；Tab / ↑↓ 选择，回车执行。
`/model ` 补全 auto、scripted 和已配置 profile。非 TTY、管道或 `TERM=dumb` 下保留普通 input，
`/` 回车显示帮助；`/sta` 回车只提示 `/status`，不会发送模型或擅自执行。
输入菜单和模型推理分离，审批仍走原授权链。输入历史只存在当前进程内存，不增加磁盘历史文件。
需要退出并重启现有 Python 进程才能加载更新，旧进程不会热更新。

流式展示依旧标为候选，只有正式完成才打印简短完成状态。精确相同正文不再重印；
工作流完成仅补上新的审查结论，审查读文件不重置编码候选的去重状态。
如果正文被改写、不同步、超出 100,000 字符比较缓冲，保守打印正式版本，不猜测相似文本相同。
失败时明确打印“运行未完成”；会话和 Trace 保留完整消息，不因为 UI 去重而删数据。
工具调用前只有空白分片时不显示 candidate 标签；正常正文前的缩进仍保留，空白缓冲也有上限。

交互实现依据 [prompt-toolkit 官方输入文档](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)，
并由真实 PTY 测试验证 `/` 打开菜单、Tab+回车执行 `/status`、`/exit` 退出。

## 离线验证

在项目根目录执行：

```bash
uv run --frozen pytest --cov=src --cov-report=term-missing \
  --cov-report=json:runs/iteration-011-coverage.json --junitxml=runs/iteration-011-tests.xml
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv lock --check --offline
uv run --frozen pi-claw eval --workers 4 \
  --output runs/iteration-011-offline-eval.json --traces-dir runs/iteration-011-offline-traces
uv run --frozen pi-claw eval-routing --output runs/iteration-011-routing-eval.json
uv build --offline --out-dir runs/iteration-011-dist
git diff --check
git diff --cached --check
```

最终结果：425 项测试通过，比基线增加 32 项；总体语句覆盖率 87%。
工程 Eval 8/8，最大同时执行 4，隔离违规 0；路由合约 6/6。
Ruff、格式、编译和依赖锁检查通过。wheel/sdist 构建通过，源码目录外安装 wheel 后的路由合约
仍为 6/6。TTY 测试在子进程运行，主进程覆盖率不会计入全部真实终端分支。

新增验证重点：默认工作区内真实创建 hello.py 并通过 uv compileall，启动根目录没有示例文件；
显式工作区覆盖、默认目录符号链接拒绝、会话按工作区隔离、查询不创建目录；
斜杠菜单不触发模型；同一正文只显示一次，完整会话仍保留原文；跨用户回合不错误吞掉相同回复。

## 真实学校模型验证

本轮获得用户授权后才调用模型，脚本、受限输出与报告仅在被忽略的 `runs/`。
工作区限定为 `projects/hello-world/`，不发送 Pi-Claw 源码，不让模型读取 Key。
授权进一步收紧到：只允许创建 AST 等价于 `print("Hello, World!")` 的 `hello.py`，
以及精确 argv `uv run python -m compileall -q hello.py`。不允许任意测试或生成代码执行。
单次冒烟最多 14 个逻辑请求、每请求只尝试 1 次、最多 2 轮审查，不更改本机持久模型配置。

第一次默认配置测试：5 次真实完整响应，51.28 秒，9331 个服务端报告 Token。
分类 1 次、规划 4 次；规划产物未通过 JSON 校验，按限额返回 `unverified`，没有生成文件。
报告为 `runs/iteration-011-live-report.json`；这次失败没有被标成成功。

随后进行了只读协议诊断：一次 economy 返回合法 JSON；qwen3.7-plus 多次返回非 JSON，
其中一次含异常重复片段，不能通过剥离 Markdown 解决。没有放宽结构化解析或自动批准。
已配置备选 qwen3.8-reasoner 的只读对照返回了有效的 list_files 工具调用，随后使用临时角色映射
检查后续链路，仍未通过：分类成功，规划模型读取空目录后返回不合规规划，下一次请求超时。
耗时 176.36 秒，4 个逻辑请求中 3 个返回完整响应，服务端已报告 Token 合计 3696；超时请求
用量未知，不能视为零。状态为 `provider_error`，没有生成文件。
报告为 `runs/iteration-011-live-auto-reasoner-report.json`。默认 planner 配置保持不变。

最后使用固定 `primary`（deepseek-flash）做隔离对照，跳过分类、规划和独立审查：
5 次真实执行请求，201.94 秒，9164 个服务端报告 Token，状态 `completed`。
实际工具顺序为 list_files → write_or_replace → run_command → read_file → 完成答复；
目标 `projects/hello-world/hello.py` 内容与预期 AST 一致，语法检查退出码为 0，
最终正文只显示一次。报告为 `runs/iteration-011-live-primary-planner-report.json`，
文件名中的 planner 只是脚本默认参数标签，固定 primary 模式没有调用规划模型。
随后由开发验证进程复查文件 AST 并实际运行，输出 `Hello, World!`；这次运行不是模型工具调用。
示例代码保留在工作区且被 Git 忽略，compileall 产生的 `__pycache__` 也被忽略。

该次实测发现工具调用前纯空白分片仍会显示空 candidate 标签，随后修正并新增回归测试，
最终 425 项测试包括该修正；没有为这处显示修正额外重复消耗真实模型请求。

结论：文件保存、受控执行、回读和正文去重已通过一个真实小样例；默认 auto 的完整
规划/编码/审查**尚未通过**真实验证。不能据此宣称模型通用能力、分类准确率或省钱比例。
需要先使用编码功能时，可临时固定已验证的角色：

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route primary --max-steps 24
```

这会跳过独立规划和审查，不等同于修复 auto 链路，也不会改写模型配置。

## 边界与 Git

- projects 是工作区/文件路径隔离，不是 OS 沙箱；用户仍需审批正常编码任务的副作用。
- 默认工作区改变不代表旧文件或会话被移动。已有 projects 内容不会清空。
- 启发式“写一个”规则改善本次误路由，不代表实现了完备的意图识别或独立权限分类器。
- 学校模型返回格式异常不归因于用户 Key 错误；连通成功也不代表所有模型均可可靠使用。
- 不自动更新默认模型角色，不把临时测试参数写回 pi-claw.toml。
- 未新增暂存、commit、合并或 push；保留前序未提交修改和原有 README 大小写重命名暂存。
