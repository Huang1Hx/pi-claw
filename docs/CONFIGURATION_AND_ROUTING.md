# 配置、交互命令与模型路由

## 根目录和配置文件

当前 `chat/run/sessions` 的默认目标为启动目录下的 `projects/`。从 Pi-Claw 根目录启动即为
`pi-claw/projects/`；文件工具只能访问这个工作区，不能访问父目录的 Pi-Claw 源码或 `.env`。
`--workspace` 可显式覆盖为已有目录。默认目录自动建立，但拒绝符号链接和同名普通文件；
`sessions` 只查询、不创建。生成内容由 Git 忽略，目录不会自动清空。

`pi-claw/` 是 Git/uv 项目根目录，`src/`、`tests/`、`docs/`、`runs/`、`README.MD`、`.env`
都直接位于根目录。源码模块直接放在 `src/` 下：`src/tools/` 管理模型可调用工具，
`src/commands/` 管理用户终端命令，`src/config/` 管理配置，`src/routing/` 管理复杂度分流与用量。
下文源码表格中的路径均相对于 `src/`，不再有 `src/pi_claw/` 中间层。

| 文件 | 用途 | Git |
|---|---|---|
| `.env.example` | 空密钥模板 | 可提交 |
| `.env` | 本机共享/独立密钥、API 地址和单模型配置 | 忽略，当前文件权限 0600 |
| `pi-claw.example.toml` | profile 与路由预算模板 | 可提交 |
| `pi-claw.ustc.example.toml` | 学校网关的规划/编码/审查模板，无 Key | 可提交 |
| `pi-claw.toml` | 本机填写的路由配置 | 忽略，通过 `--config` 显式加载 |
| `pyproject.toml`、`uv.lock` | 包元数据和 uv 依赖锁 | 可提交 |

`.env` 是隐藏文件；macOS Finder 中按 `Command + Shift + .` 显示。VS Code 文件树可直接打开。
新克隆仓库先执行：

```bash
cd /ABS/PATH/pi-claw
uv sync --frozen
cp .env.example .env
chmod 600 .env
```

本轮工作区已经创建 `.env`，直接填写即可。Python 虚拟环境是 uv 管理的 `.venv/`，与存储变量
的 `.env` 不是同一个文件。CLI 从**启动目录**读取 `.env`，不会根据 `--workspace` 自动搜索
目标仓库的密钥文件。只在可信启动目录运行，或用 `--env-file /ABS/PATH/.env` 指定本机配置。

优先级为 `.env < 进程环境 < 显式 --model/--base-url`；后两项只用于单模型入口。
`.env` 解析支持 `KEY=value`、单/双引号、注释和 `export KEY=value`，不执行 shell、不展开变量、
不加载任意配置脚本。不需要 `.env` 时用 `--no-env`。配置解析不会把密钥加入全局进程环境。

## 单模型和多模型

单模型填写 `.env` 的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 后运行：

```bash
uv run --frozen pi-claw chat --provider openai
```

接 Kimi/Qwen/DeepSeek 时这三个变量也可使用对应厂商的值。获取入口见
[API Key 指南](API_KEY_GUIDE.md)。模型 ID、端点、密钥必须来自同一服务。

### 同一 API Key 切换不同模型

路由模式先复制 `pi-claw.example.toml` 为 `pi-claw.toml`。若不同模型属于同一服务/网关，
在 `.env` 只填写一套连接：

```dotenv
OPENAI_API_KEY=<YOUR_SHARED_API_KEY>
OPENAI_BASE_URL=<YOUR_GATEWAY_BASE_URL>
```

`pi-claw.toml` 通过 `[connection]` 引用共享连接，各 profile 只填不同的 model ID：

```toml
[connection]
base_url_env = "OPENAI_BASE_URL"
api_key_env = "OPENAI_API_KEY"

[routing]
cheap = "economy"
strong = "primary"
intent_strategy = "model"
max_cheap_requests = 2
max_requests = 12
max_output_tokens = 2048

[models.economy]
model = "<YOUR_LOW_COST_TOOL_MODEL_ID>"

[models.primary]
model = "<YOUR_PRIMARY_TOOL_MODEL_ID>"
```

轻量与主力请求使用相同的 URL 和 Authorization，通过 `model` 字段切换。
轻量模型的分类请求没有工具，执行请求的工具 Schema 为只读集合。
`economy`、`primary` 是你指定的能力角色名，不是服务端模型 ID，也不是系统查询价格的结果。
`OPENAI_MODEL` 只用于单模型入口，路由模式必须为每个 profile 显式填写 `model`。
所有模型 ID 必须由同一平台/网关提供并且当前 Key 有权访问；复用 Key 不代表不同厂商的
官方密钥可以互换。Kimi/Qwen/DeepSeek 若通过同一平台访问，应填写该平台要求的准确 ID。

共享地址也可直接写成 `[connection]` 下的 `base_url = "https://你的服务地址/v1"`，但必须
删除 `base_url_env`，二者不能同时填写。真实 Key 不得写在 TOML 中。
环境变量优先级沿用 `.env < 进程环境`，缺失共享 Key 时在请求前报错，不从其他变量借用密钥。

### 独立连接和旧配置兼容

不设置 `[connection]` 时，沿用每个 profile 自己填写 `base_url` 和 `api_key_env` 的旧格式。
设置共享连接后，某个 profile 也可覆盖整套连接：

```toml
[models.external]
base_url_env = "OTHER_SERVICE_BASE_URL"
api_key_env = "OTHER_SERVICE_API_KEY"
model = "<YOUR_OTHER_SERVICE_MODEL_ID>"
max_attempts = 1
```

profile 的地址可直接写 `base_url`，也可通过 `base_url_env` 引用环境变量，二者只能选一个。
只覆盖地址或只覆盖 Key 变量均拒绝加载，避免更换服务时无意携带共享凭据。
完整覆盖中的 Key 缺失也不会退回共享 Key。所有占位符必须替换。
不需要填写价格；不会因为 Key 相同就把两个不同模型的 Token 合并为同一模型。

### 启动

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route auto
uv run --frozen pi-claw chat --config pi-claw.toml --route primary
uv run --frozen pi-claw run "列出项目文件" --config pi-claw.toml --route auto
```

`--route auto` 启用自动选择；`--route <profile>` 固定模型。路由模式不与 `--model/--base-url`
混用。恢复已配置路由的 Session 时仍须提供 `--config`；正常退出提示会携带原配置路径。
启用自动路由表示同意把当前活动上下文发送到所配置的模型服务；升级时保留工具反馈。
共享连接模式是同一端点的不同模型，独立连接模式则可能涉及不同供应商。

## 工具与终端命令

学校多模型工作流通过 `[workflow] enabled=true` 加入自动路由：复杂分支进入
planner → primary → reviewer，命中风险规则后再进入 premium_reviewer；简单任务保留轻量路径。
普通角色使用学校 DeepSeek，高级审查使用独立 Kimi 连接。模型 ID、profile 推理参数、
请求预算和精确启动方式见 [多模型工作流](WORKFLOW_PIPELINE.md)。旧配置默认不启用，
固定 `--route <profile>` 绕过工作流。每个模型的 `max_output_tokens` 覆盖 routing 默认值，
`timeout_seconds` 设置单次 HTTP 超时，`max_attempts` 可单独收紧付费模型重试；分类仍最多 128 Token。

| 源文件 | 模型工具名/用户命令 | 行为 |
|---|---|---|
| `tools/read.py` | `read_file` | 有界读取 |
| `tools/write.py` | `write_or_replace` | 独占创建或唯一文本替换 |
| `tools/edit.py` | `apply_patch` | 全部校验后一次写回的多段替换 |
| `tools/grep.py` | `search_text`、`list_files` | 字面量搜索与有界枚举，不执行系统 grep |
| `tools/shell.py` | `run_command` | 白名单 argv 子进程，经审批 |
| `commands/status.py` | `/status [--verbose]` | 默认显示五项简明状态，verbose 保留原始路由详情 |
| `commands/model.py` | `/model [模型ID\|auto\|别名\|scripted]` | 查看实际模型与分工，ID 唯一才直接选择，切换时创建新 Session |
| `commands/sessions.py` | `/sessions`、`/resume`、`/new`、`/title` | 会话管理 |
| `commands/help.py`、`exit.py` | `/help`、`/exit` | 元数据帮助与唯一退出命令 |
| `commands/context.py` | `/context`、`/compact`、`/history` | 活动上下文 |
| `commands/usage.py` | `/usage` | 当前会话与当前工作区所有会话的已记录 Token；缺口单独显示 |

保留原工具协议名可以继续恢复旧 Session。用户命令不会作为提示词进入模型，`help` 由注册表
生成。没有自动加载仓库内自定义命令。`/model` 切换产生新 Session，原会话和审批状态保留在旧
Session 中；新会话不继承旧模型对话和会话审批。

交互 TTY 使用 `prompt-toolkit` 的命令菜单：输入 `/` 立即列出命令，`/sta` + Tab 补全 `/status`，
`/model ` 补全具体模型 ID，配置别名的说明也展示实际模型。方向键/Tab 只选候选，回车才执行。
`/model <ID>` 只选择已有配置，不为未知 ID 生成连接；ID 重名需显式选别名，已有别名优先匹配。
`/exit` 是唯一退出命令。非 TTY 仍可 `/` 回车查看帮助；
不完整命令显示本地补全建议，绝不作为用户任务发送模型。输入历史不额外写入磁盘。
旧根目录会话须显式 `--workspace .` 恢复；默认新工作区不会继承旧工作区的消息或审批范围。

## 自动路由状态

1. 新模板设置 `intent_strategy = "model"`，每回合先由 economy 判断当前任务复杂度。
   只发送固定分类提示与本次任务，不附带仓库文件、工具 Schema 或完整会话；输出最多
   `min(128, max_output_tokens)` Token，且不流式展示分类原文。
2. `routing/classifier.py` 只接受 `{"complexity":"simple"}` 或 `{"complexity":"complex"}`。
   简单意图识别、给定分支选择、短文本提取、概念解释和单步只读请求可交 economy；代码修改、
   调试、架构与多步骤推理交 primary。没有接入外部模型价格目录，也不自动评估模型能力。
3. 畸形 JSON、重复字段、工具候选、分类网络错误等均保守转 primary；超出 4000 字符跳过分类，
   原始任务仍交执行模型。明确修改/运行等副作用关键词和“继续”等缺失上下文请求有宿主兜底，
   即使分类报 simple 也不降级。这些启发式限制不是完备的意图或安全判定。
4. economy 的执行请求仅收到 `read_file/list_files/search_text` 的工具 Schema。
5. 若 economy 仍返回写入、命令或未知工具，Router 丢弃这组候选、记录 usage，再让 primary
   基于原上下文重新决策；此时工具没有执行。
6. 真实工具失败、宿主完成验收拒绝或 economy 达到执行请求次数限制时升级 primary。
7. 每回合只分类一次，工具反馈不会再分类；升级保持到回合结束。下一回合重新判断。
   固定 profile 模式不分类、不自动切换或过滤只读工具；两个角色指向同一 profile 时跳过分类。
8. 无论选哪个模型，最终执行仍须经过 ToolExecutor → Policy → Approval → Workspace。

分类输出只用于宿主选分支，不作为 assistant 回复写入 Session；分类事件和用量照常记录。
分类没有判断真实任务已完成的权力。分类失败 fallback 会产生 `intent_classified` 事件，
只含固定原因，不持久化服务错误原文。若配置独立服务，fallback 可能把任务交给主力供应商。
执行阶段的网络/鉴权错误不会自动跨 profile 重试，原有有限重试与“部分流不重放”保持有效。

旧配置省略策略，或显式设置 `intent_strategy = "rules"`，保留 `routing/intent.py` 的本地
短只读规则，不增加分类模型调用。`--route primary` 可强制使用主力且跳过分类。

## 用量与请求上限（不需要价格）

每回合 `RunResult.routing` 包括 `requests`（逻辑请求尝试）、`classification_requests`、
`classification_failures`（含过长任务跳过分类）、`cheap_requests`（轻量执行次数）、
`intent`（标签及来源）、`escalations` 和按模型的完整响应用量。
Trace 持久化 `model_routed/intent_classified/route_escalated/routing_summary`，
`model_routed.purpose` 区分 `classification/execution`；开启工作流时另有
`planning/coding/reviewing`，`requests_by_purpose` 记录各阶段调用数。
分类失败也不会从请求上限中消失。
分类返回完整响应时，其 usage 加入 Agent 总用量，原文不进入正式会话。缺失 usage 通过
`RunResult.usage.requests_without_usage` 体现；失败请求的服务端 Token 不猜测为零。
未配置价格和金额预算时不输出 `known_cost/currency/unknown_requests`，只记录调用与 Token。
`/status` 展示简明运行状态，`/status --verbose` 展示本次会话载入后最近回合的路由统计；
`/usage` 显示当前 Session 和项目（当前工作区所有会话）的输入/输出/总 Token。
当前会话已包含在项目累计中；更换工作区或会话数据目录会改变范围，不递归合并子项目工作区。
无账本、坏账本和无 usage 响应均有明确提示，不把统计缺口解释成零消耗。它不是账户余额或账单。

`max_requests` 限制逻辑模型请求数，包含分类和被丢弃的 economy 副作用候选；每个逻辑请求最多还有
`max_attempts` 次网络尝试。`max_output_tokens` 以 `max_tokens` 字段发送给服务，兼容性待联调。
二者提供独立的资源上限；`max_cheap_requests` 仅限制轻量执行，不包含分类。

### 历史金额配置兼容

旧配置主动提供 `input_per_million/output_per_million` 时仍能按服务端 usage 算术估算，
但单价不参与复杂度判断或模型选择。没有内置报价、自动汇率换算或缓存折扣计算。
新模板不提供这些字段，也不需要用户补价格。

可选 `max_cost` 在已知费用达到阈值时阻止**下一次**请求。无法提前精确知道输入分词和失败
请求账单，因此单次请求可能越过剩余额度。费用未知、缺少报价或出现重试时，启用金额预算的
回合会停止继续请求；它不是供应商侧硬扣费上限。

## 离线对照与真实验证

```bash
uv run --frozen pi-claw eval-routing --output runs/routing-eval.json
```

固定模型替身分别运行 auto 与全 primary，覆盖只读、复杂/含糊任务、工具失败升级和副作用
拦截。每个运行使用独立临时工作区、实际工具和宿主断言；报告包括分类/总请求、主力执行请求、
升级数、Token、本地 P95 和逐运行 Trace。不使用假设价格，也不输出费用节省率。
v2 夹具共 6 个场景，auto 共 20 次请求（含 6 次分类）、主力执行 8 次；固定主力共 12 次请求。
分类标签是预设替身输出，通过率不是实际模型分类准确率；分类开销使总请求可能增加。

已完成的受限真实模型联调及失败边界见
[Iteration 011](iterations/ITERATION_011_WORKSPACE_TERMINAL.md)，不等同于路由质量评测。
后续应在同一任务集上对比 auto 与固定 primary，至少记录：
宿主验收通过率、分类准确率、复杂任务错误降级率、虚假完成率、分模型 Token、分类开销、
主力请求占比、P95 延迟、升级率和请求上限中断率。价格未知时不报告费用或省钱百分比。

## 参考

- [Pichu Commands 模块](https://github.com/yeabwang/pichu/blob/main/docs/commands-module.md)
- [Pichu Config 模块](https://github.com/yeabwang/pichu/blob/main/docs/config-module.md)
- [Pichu Tools 模块](https://github.com/yeabwang/pichu/blob/main/docs/tool-management.md)

采用职责分层作为参考；配置不使用全局单例，命令不自动加载第三方文件。复杂度路由为本项目独立
增加的功能，不声称 Pichu 具有同样策略。
