# Pi-Claw 多供应商 API Key 获取与配置指南

本指南覆盖 OpenAI、Kimi、Qwen/阿里云百炼和 DeepSeek。Pi-Claw 的命令行参数 `--provider openai` 表示使用项目内置的 **OpenAI-compatible Chat Completions 协议适配器**，不等于只能连接 OpenAI 官方服务。

无论准备使用哪家供应商，都先跑离线版本。模型、价格、地域、业务空间和接口权限会变化；本文只给出官方获取入口和映射方法，不承诺免费额度或固定价格。供应商资料核对日期：**2026-08-11**。

## 先运行离线版本：完全不需要 API Key

Pi-Claw 的离线演示和测试不访问任何模型服务，不需要 OpenAI、Kimi、Qwen、DeepSeek 或其他第三方 API Key。第一次使用时先验证这两条路径：

```bash
uv run pi-claw demo --yes
uv run pytest
```

`demo --yes` 使用项目内置的确定性 Scripted Provider，在临时工作区完成读取、修复和测试。`pytest` 验证 Agent 循环、工具、安全策略和轨迹等本地逻辑。只有这两项通过后，再排查真实模型接入，可以避免把项目错误与外部 API 错误混在一起。

> `--yes` 只批准演示临时目录中的白名单操作。普通 `run` 命令默认仍是只读模式。

## 什么时候才需要真实 Provider

以下情况才需要 API Key：

- 使用 `--provider openai` 让真实模型自主决定下一步或选择工具；
- 验证真实模型的 Chat Completions、结构化 Tool Call 和多轮工具循环兼容性；
- 比较不同模型在固定任务集上的质量、延迟和错误率。

真实模型主入口使用 `chat --print` 或交互式 `chat`，因为它包含 Session、项目 Prompt、活动
Context 和终端审批。`run` 仅作为兼容 v0 的单轮入口保留。

真实会话会保存 Provider、模型和 Base URL 以供 `--continue` 恢复，但不会保存 API key。
重新打开终端后，仍需先加载对应供应商的专属 Key 环境变量。

在交互 REPL 中执行 `/resume <SESSION_ID>` 时，如果目标会话的 Provider、模型或 Base URL 与
当前运行时不同，Pi-Claw 会拒绝静默切换，并打印一条显式的 `uv run pi-claw chat ...` 重启
命令。先映射该会话对应供应商的 Key，再运行提示命令，避免把一家 Key 发给另一家端点。

以下情况不需要 API Key：

- `uv run pi-claw demo --yes`；
- `uv run pytest`；
- 默认的 `uv run pi-claw run "..."`，因为默认 Provider 是完全离线的 `scripted`；
- 使用已有 JSONL 轨迹进行 `replay`。

## 供应商选择与映射

Pi-Claw 当前统一读取这三个变量：

- `OPENAI_API_KEY`：当前选中供应商的 Key；
- `OPENAI_MODEL`：当前供应商中支持 Chat Completions Tool Call 的准确模型 ID；
- `OPENAI_BASE_URL`：当前供应商的 OpenAI-compatible API 根地址，OpenAI 官方可省略。

Base URL 必须是带主机名的 `http://` 或 `https://` API 根地址，不能内嵌用户名/密码，也不能带
query（`?...`）或 fragment（`#...`）。这样可避免明显凭据进入 Session 元数据；API key 仍只能
放在环境变量中。

国内供应商有各自的官方 Key 变量。推荐先保存专属变量，再在**当前终端**映射到 Pi-Claw 使用的 `OPENAI_*`。这样能清楚看到当前选中哪家服务，也能避免把 A 厂商的 Key 发给 B 厂商的 Base URL。

每次切换供应商都重新执行对应小节的完整映射，不要只改其中一项。所有 `<...>` 都是醒目占位符，必须替换为控制台当前真实值，且不能把尖括号原样保留。

### OpenAI

#### 官方获取步骤

1. 打开 [OpenAI API Keys](https://platform.openai.com/api-keys)。
2. 登录正确的 OpenAI Platform 账号或组织，在 Dashboard 创建 Secret Key。
3. 只在本地安全保存 Key，不要把它发到聊天、Issue、截图、轨迹或 Git。
4. 从当前账号的官方模型文档或控制台选择支持 Chat Completions Tool Call 的准确模型 ID。

官方参考：[OpenAI API Quickstart](https://developers.openai.com/api/docs/quickstart)。ChatGPT 产品登录或订阅不应被当成 API 调用已经可用的证明，实际权限、用量和计费状态以当前 Platform 账号为准。

#### 专属变量和当前终端映射

OpenAI 官方变量就是 `OPENAI_API_KEY`。切回 OpenAI 时必须清除其他供应商留下的 Base URL：

```bash
export OPENAI_API_KEY="<YOUR_OPENAI_API_KEY>"
export OPENAI_MODEL="<OPENAI_CHAT_COMPLETIONS_TOOL_MODEL_ID>"
unset OPENAI_BASE_URL
```

项目会使用默认根地址 `https://api.openai.com/v1`，并调用 `/chat/completions`。

#### Pi-Claw 只读验证

```bash
mkdir -p runs

uv run pi-claw chat \
  "只读列出工作区中最重要的文件，不要修改。" \
  --print \
  --provider openai \
  --workspace "/ABSOLUTE/PATH/TO/SAFE_WORKSPACE" \
  --trace "runs/openai-smoke.jsonl"
```

#### 兼容性检查

- 模型必须支持 Chat Completions 的 `messages`、`tools`、`tool_choice` 和 `tool_calls`。
- Pi-Claw 当前不直接调用 Responses API；不要把 Responses 端点配置成 Base URL。
- Pi-Claw v1 可解析同轮多个标准 Tool Call，并按返回顺序执行；有依赖关系的动作仍应分轮进行。

### Kimi（Moonshot AI 国内平台）

#### 官方获取步骤

1. 打开 [Kimi API 官方概览](https://platform.kimi.com/docs/api/overview)。
2. 从官方文档进入控制台，在当前账号的 API Key 管理页面创建或管理 Key。
3. 在控制台核对当前可用且支持 Tool Call 的准确模型 ID，不沿用教程中的旧模型名。
4. 官方专属环境变量名为 `MOONSHOT_API_KEY`。

#### 专属变量和当前终端映射

```bash
export MOONSHOT_API_KEY="<YOUR_MOONSHOT_API_KEY>"

export OPENAI_API_KEY="$MOONSHOT_API_KEY"
export OPENAI_BASE_URL="https://api.moonshot.cn/v1"
export OPENAI_MODEL="<KIMI_CHAT_COMPLETIONS_TOOL_MODEL_ID>"
```

#### Pi-Claw 只读验证

```bash
mkdir -p runs

uv run pi-claw chat \
  "只读列出工作区中最重要的文件，不要修改。" \
  --print \
  --provider openai \
  --workspace "/ABSOLUTE/PATH/TO/SAFE_WORKSPACE" \
  --trace "runs/kimi-smoke.jsonl"
```

#### 兼容性检查

- 国内官方根地址是 `https://api.moonshot.cn/v1`；Pi-Claw 会追加 `/chat/completions`，最终端点为 `/v1/chat/completions`。
- 当前模型必须明确支持 Tool Call，并返回 OpenAI-compatible `choices[0].message.tool_calls`。
- Key、模型和国内 Base URL 必须来自同一个当前账号环境。

### Qwen（阿里云百炼）

#### 官方获取步骤

1. 按 [阿里云百炼获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key) 创建 Key。
2. 阅读 [Qwen OpenAI Chat Completions 兼容文档](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。
3. 创建时确认地域和业务空间，并从创建成功弹窗或对应地域官方文档复制 API Key 与 API Host。
4. 官方专属环境变量名为 `DASHSCOPE_API_KEY`。

Qwen/百炼的 Key、API Host、地域和业务空间有关联，不能只复制别人的 Base URL。北京地域的官方地址形态示例为：

```text
https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

上面只是带 `{WorkspaceId}` 的地域形态示例，不是可直接运行的地址。必须使用本次创建成功弹窗或当前地域官方页面给出的完整值。

#### 专属变量和当前终端映射

```bash
export DASHSCOPE_API_KEY="<YOUR_DASHSCOPE_API_KEY>"

export OPENAI_API_KEY="$DASHSCOPE_API_KEY"
export OPENAI_BASE_URL="<DASHSCOPE_BASE_URL_COPIED_FROM_YOUR_REGION_AND_WORKSPACE>"
export OPENAI_MODEL="<QWEN_CHAT_COMPLETIONS_TOOL_MODEL_ID>"
```

#### Pi-Claw 只读验证

```bash
mkdir -p runs

uv run pi-claw chat \
  "只读列出工作区中最重要的文件，不要修改。" \
  --print \
  --provider openai \
  --workspace "/ABSOLUTE/PATH/TO/SAFE_WORKSPACE" \
  --trace "runs/qwen-smoke.jsonl"
```

#### 兼容性检查

- Base URL 必须来自 Key 所属的当前地域和业务空间，通常以 `/compatible-mode/v1` 结束。
- 当前模型必须支持 OpenAI Chat Completions Tool Call；不要仅凭“Qwen”系列名推断所有模型都支持。
- 401/404 时同时核对 Key、地域、WorkspaceId、API Host 和模型权限，不能只轮换 Key。

### DeepSeek

#### 官方获取步骤

1. 打开 [DeepSeek API 官方文档](https://api-docs.deepseek.com/zh-cn/)。
2. 从官方文档进入 [DeepSeek Platform](https://platform.deepseek.com/)，创建和管理 API Key。
3. 从当前控制台或官方文档选择支持 Tool Call 的准确模型 ID，不固定使用教程中的旧 ID。
4. 官方专属环境变量名为 `DEEPSEEK_API_KEY`。

#### 专属变量和当前终端映射

```bash
export DEEPSEEK_API_KEY="<YOUR_DEEPSEEK_API_KEY>"

export OPENAI_API_KEY="$DEEPSEEK_API_KEY"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="<DEEPSEEK_CHAT_COMPLETIONS_TOOL_MODEL_ID>"
```

#### Pi-Claw 只读验证

```bash
mkdir -p runs

uv run pi-claw chat \
  "只读列出工作区中最重要的文件，不要修改。" \
  --print \
  --provider openai \
  --workspace "/ABSOLUTE/PATH/TO/SAFE_WORKSPACE" \
  --trace "runs/deepseek-smoke.jsonl"
```

#### 兼容性检查

- 官方根地址是 `https://api.deepseek.com`；Pi-Claw 会追加 `/chat/completions`。
- 当前模型必须支持 Tool Call，并返回 Pi-Claw 所需的 OpenAI-compatible Chat Completions 格式。
- 不要根据旧教程固定模型名、价格或额度，以当前官方控制台和文档为准。

### 选择后检查映射，不打印 Key

```bash
test -n "$OPENAI_API_KEY" && echo "当前 Provider Key 已映射" || echo "Key 未映射"
printf 'OPENAI_MODEL=%s\n' "$OPENAI_MODEL"
printf 'OPENAI_BASE_URL=%s\n' "${OPENAI_BASE_URL:-https://api.openai.com/v1（OpenAI 默认值）}"
```

不要使用 `echo "$OPENAI_API_KEY"`。尤其不要只改 `OPENAI_BASE_URL` 而保留上一家供应商的 `OPENAI_API_KEY`。

## macOS zsh 持久配置：只保存专属 Key

不要把真实 Key 写进项目目录，也不要长期保存 `OPENAI_MODEL` 和 `OPENAI_BASE_URL`。建议把各家专属 Key 放在用户目录的独立文件中，并限制权限：

```bash
mkdir -p "$HOME/.config/pi-claw"
touch "$HOME/.config/pi-claw/provider-keys.zsh"
chmod 600 "$HOME/.config/pi-claw/provider-keys.zsh"
open -e "$HOME/.config/pi-claw/provider-keys.zsh"
```

只填写实际使用的供应商，未使用的行保持注释或删除：

```bash
# OpenAI 的官方专属变量本身就是 OPENAI_API_KEY。
export OPENAI_API_KEY="<YOUR_OPENAI_API_KEY>"
export MOONSHOT_API_KEY="<YOUR_MOONSHOT_API_KEY>"
export DASHSCOPE_API_KEY="<YOUR_DASHSCOPE_API_KEY>"
export DEEPSEEK_API_KEY="<YOUR_DEEPSEEK_API_KEY>"
```

此文件只保存专属 Key，不保存模型或 Base URL。然后在用户级 `~/.zshrc` 中加入：

```bash
source "$HOME/.config/pi-claw/provider-keys.zsh"
```

重新打开终端，或在当前终端加载配置：

```bash
source "$HOME/.zshrc"
```

每次选择供应商时，仍执行对应小节的 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL` 映射。切回 OpenAI 时可重新 `source` 专属 Key 文件恢复 OpenAI Key，然后执行 `unset OPENAI_BASE_URL`。

如果电脑账号会被多人共用，不建议自动加载长期 Key；优先使用当前终端临时配置或操作系统提供的安全凭据存储。

## `.env` 的使用边界

项目的 `.gitignore` 已包含 `.env`，但 Git 忽略不是密钥保险箱，也不能保护已经提交过的文件。提交前检查：

```bash
git check-ignore -v .env
git status --short
```

当前 Pi-Claw 不会自动读取 `.env`。如自行创建 `.env`，仍需由可信的本地 shell 或其他配置工具把变量加载到环境中。不要为了图方便给项目加入会打印全部环境变量的调试代码。

可以提交无真实值的 `.env.example` 作为字段说明，但真实 `.env`、Key、含 Key 的日志或 shell 记录都不能提交。

## 运行前必须理解的接口兼容要求

OpenAI 官方 Quickstart 当前使用 Responses API 介绍新项目，而 Pi-Claw 当前 Provider 实现调用的是：

```text
POST <OPENAI_BASE_URL>/chat/completions
```

默认会得到：

```text
POST https://api.openai.com/v1/chat/completions
```

因此，当前 Pi-Claw 需要 Provider 同时满足：

- 支持 Chat Completions，而不只是 Responses API；
- 接受 `messages`、`tools` 和 `tool_choice`；
- 返回 `choices[0].message`；
- 工具调用使用 `message.tool_calls[].function`，其中 `arguments` 是 JSON 对象的字符串表示；
- 可以返回同轮多个标准 Tool Call；Pi-Claw v1 会顺序执行并为每个调用追加独立 ToolResult。
- 成功响应和 HTTP 错误响应都必须在客户端读取上限内；当前默认上限为 `5,000,000` 字节。

不要把 Responses API 地址填入 `OPENAI_BASE_URL`。该变量应是 API 根地址；Pi-Claw 会自动追加 `/chat/completions`。如果传入值已经以 `/chat/completions` 结尾，项目不会重复追加。

不要把 Key 写成 `https://user:key@example.com/v1`，也不要在 Base URL 后追加 `?api_key=...` 或
`#...`。这类地址会在 Session 元数据校验时被拒绝。模型响应或错误响应超过默认字节上限时也会
安全失败；此时应检查 Provider 是否返回了异常页面或过大错误体，而不是提高上限后盲目重试。

模型名称也不能凭教程猜测。请从当前 Provider 的官方模型文档或账号控制台选择准确模型 ID，并确认其支持 Chat Completions 工具调用。

## 所有供应商验证后的共同检查

把各供应商命令中的 `/ABSOLUTE/PATH/TO/SAFE_WORKSPACE` 替换成明确的绝对路径。首次验证不要添加 `--yes`，保持只读。成功时至少检查：

- 命令输出中出现审计轨迹路径；
- Agent 完成只读工具调用并返回总结；
- 对应的 `runs/*-smoke.jsonl` 可以回放；
- 轨迹中没有 API Key。

回放命令：

```bash
uv run pi-claw replay "runs/<PROVIDER_NAME>-smoke.jsonl"
```

只有只读验证成功后，才在专用测试工作区中考虑添加 `--yes`。不要第一次就对重要仓库批准写入或命令执行。

## 常见错误排查

### HTTP 401：未授权

常见原因：

- Key 错误、已吊销、已轮换或复制时带入空格；
- 环境变量没有加载到当前终端；
- 使用第三方 Base URL，却仍提供 OpenAI 的 Key，或反过来；
- Provider 要求的认证方式并非 Bearer Key。

检查顺序：

```bash
test -n "$OPENAI_API_KEY" && echo "Key 变量存在" || echo "Key 变量缺失"
printf 'Base URL: %s\n' "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
```

不要输出完整 Key。确认账号和服务地址匹配；仍失败时，在 Dashboard 吊销旧 Key 并创建新 Key。

### HTTP 404：地址、端点或模型不匹配

常见原因：

- `OPENAI_BASE_URL` 路径写错；
- 把 Responses API 的端点当成 Chat Completions 根地址；
- 第三方兼容服务并不提供 `/chat/completions`；
- 模型 ID 拼写错误，或当前账号/端点不可用。

Pi-Claw 会自动在 Base URL 后追加 `/chat/completions`。先确认最终应访问的端点，再核对模型 ID。不要通过反复猜 URL 解决 404。

### HTTP 429：限流或用量不可用

429 可能表示请求速率限制、并发限制、配额或当前账号用量状态问题。处理方式：

1. 停止连续重试，避免扩大问题；
2. 查看 Provider 控制台当前限制和用量状态；
3. 降低请求频率，稍后重试；
4. 开发期间切回离线 `demo`、`scripted` 和测试。

不要在没有退避和最大次数限制的情况下自动无限重试。

### 模型不支持工具调用

可能表现为：

- API 明确拒绝 `tools` 或 `tool_choice`；
- 模型只返回普通文本，始终不产生 `tool_calls`；
- 返回字段看似相似，但不符合 Chat Completions 的 `choices[0].message.tool_calls`；
- 返回了非标准、多层嵌套或参数不是 JSON 对象的 Tool Call，因协议不兼容被拒绝。

处理方式：确认模型支持 Chat Completions 工具调用；用准确模型 ID 更新 `OPENAI_MODEL`；检查兼容服务的响应格式。不要仅因为服务自称“OpenAI-compatible”就假设工具调用完全兼容。

### 连接超时或 TLS/DNS 错误

先检查网络、代理、DNS 和 Base URL，不要立刻轮换 Key。Key 错误通常对应鉴权响应，而不是 DNS 解析失败。若公司网络使用代理或证书检查，按组织安全规范配置，不要关闭 TLS 验证来绕过问题。

## 其他 OpenAI-compatible 第三方 Provider

Kimi、Qwen 和 DeepSeek 已在前文给出单独映射。其他服务同样只能使用其官方文档提供的值，本指南不杜撰地址、模型名、价格或免费额度：

```bash
export OPENAI_API_KEY="<THIRD_PARTY_API_KEY>"
export OPENAI_MODEL="<THIRD_PARTY_CHAT_COMPLETIONS_TOOL_MODEL_ID>"
export OPENAI_BASE_URL="<THIRD_PARTY_OPENAI_COMPATIBLE_API_ROOT>"

uv run pi-claw chat \
  "只读列出工作区文件，不要修改。" \
  --print \
  --provider openai \
  --workspace "/ABSOLUTE/PATH/TO/SAFE_WORKSPACE" \
  --trace "runs/third-party-smoke.jsonl"
```

接入前逐项核对第三方官方文档：

1. Base URL 是否为 API 根地址；
2. 是否实现 `/chat/completions`；
3. 模型是否支持 `tools`、`tool_choice` 和标准 `tool_calls`；
4. 认证是否使用 `Authorization: Bearer <key>`；
5. 返回格式是否包含 `choices[0].message`；
6. 数据保留、费用、速率限制和地区要求是否可接受。

如果任一项不兼容，需要修改或新增 Provider 适配器，不能只靠更换环境变量解决。

## Key 轮换、吊销与泄漏处理

正常轮换：

1. 在 Provider Dashboard 创建新 Key；
2. 更新用户目录的本地配置；
3. 新开终端完成只读验证；
4. 验证成功后吊销旧 Key；
5. 删除不再需要的本地副本。

一旦怀疑泄漏：

1. 立即在 Dashboard 吊销 Key，不要等待确认；
2. 创建新 Key，并更新本地环境；
3. 检查 Git 工作区、Git 历史、终端历史、日志、轨迹、Issue 和截图；
4. 若 Key 曾进入 Git 历史，仅删除当前文件不够，需要按团队流程清理历史并通知相关人员；
5. 检查异常调用和费用，再恢复真实 Provider 测试。

不要把 Key 放在任务文本、`--trace` 路径、异常截图或测试夹具中。Pi-Claw 会尽力清理 Provider 错误中的 Key，但调用方仍应从源头避免暴露。

## 最小推荐流程

```text
离线 demo/test 通过
  -> 对应供应商控制台创建 Key
  -> 保存专属变量
  -> 当前终端映射 OPENAI_* 三项
  -> 只读真实 Provider 冒烟测试
  -> 检查并回放脱敏轨迹
  -> 需要长期使用时再配置用户级安全文件
  -> 定期轮换，不再使用时立即吊销
```

如果真实 Provider 失败，先保留离线路径继续开发，并明确记录失败属于鉴权、模型兼容、网络、限流还是 Pi-Claw 逻辑；不要把“API 不可用”写成“项目所有功能不可用”。
