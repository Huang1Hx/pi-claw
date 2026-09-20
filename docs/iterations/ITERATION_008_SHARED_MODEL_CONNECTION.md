# Iteration 008：同一 API Key 下切换不同 model

## 状态与基线

日期：2026-09-14。状态：`待用户审核`。分支：`refactor/package-layout`。
基线为 Iteration 007 的 283 项通过测试。用户要求模型路由共用 API Key，通过不同 model
选择模型。本轮只调整配置解析及模板，不改变意图规则、升级策略、审批边界或会话协议。

原运行时已按 profile 的 model 构造请求，且原格式允许多个 profile 引用相同密钥变量，
但连接信息需要重复填写，模板也默认使用不同供应商的 Key。本轮把共享连接显式提取出来。

## 新配置

`.env` 保存共享连接，真实文件不提交：

```dotenv
OPENAI_API_KEY=<YOUR_SHARED_API_KEY>
OPENAI_BASE_URL=<YOUR_GATEWAY_BASE_URL>
```

复制根目录 `pi-claw.example.toml` 为 `pi-claw.toml`，填写同一平台支持的准确模型 ID：

```toml
[connection]
base_url_env = "OPENAI_BASE_URL"
api_key_env = "OPENAI_API_KEY"

[models.economy]
model = "<YOUR_LOW_COST_TOOL_MODEL_ID>"

[models.primary]
model = "<YOUR_PRIMARY_TOOL_MODEL_ID>"
```

```bash
uv run --frozen pi-claw chat --config pi-claw.toml --route auto
```

`economy/primary` 是本地角色名，真正发给模型服务的是对应的 `model` 值。
两个请求复用 URL 和 Authorization；自动升级会改变 model，并按既有规则改变可见工具集。
共享 Key 只适用于同一平台/网关有权限访问的模型，不意味着不同厂商官方 Key 可以互换。
这是服务端模型选择，不是在本机下载或加载权重。路由不读取单模型变量 `OPENAI_MODEL`。

## 修改清单

| 文件 | 改动 |
|---|---|
| `src/config/connections.py` | 新增共享连接解析；支持直接 base_url 或 base_url_env 二选一，Key 只引用 api_key_env；校验地址、端口与敏感参数，错误不回显原值 |
| `src/config/loader.py` | 接受顶层 connection；为省略连接字段的 profile 复用已解析地址与 Key；保留逐 profile 独立配置 |
| `src/config/models.py` | 说明 ModelProfile 保存解析后的连接，多个模型可以共享连接且保持独立 model 和报价 |
| `src/routing/router.py` | 补充按所选 profile 构造请求、避免全局模型状态切换的中文说明；路由行为不变 |
| `pi-claw.example.toml` | 默认采用共享连接；economy/primary 只填 model 和可选价格，另提供独立连接覆盖示例 |
| `.env.example` | 区分共享 Key/地址、单模型 OPENAI_MODEL 与可选供应商专属 Key |
| `tests/test_shared_model_connection.py` | 新增 29 项离线用例，覆盖配置、安全拒绝、模板可用性、同步与 SSE 请求级升级 |
| `README.MD` | 更新模型路由快速配置和请求字段说明 |
| `docs/CONFIGURATION_AND_ROUTING.md` | 完整说明共享配置、变量优先级、独立覆盖和启动步骤 |
| `docs/ARCHITECTURE.md`、`docs/WORKFLOW_AND_DATA_FLOW.md` | 同步连接解析模块和模型选择路径 |
| `docs/API_KEY_GUIDE.md`、`docs/SECURITY.md` | 更新共享 Key 用法与跨服务凭据隔离边界 |

## 兼容与安全取舍

- 不设置 `[connection]` 的旧 TOML 继续要求各 profile 填写自己的 `base_url/api_key_env`。
- 使用共享连接时，profile 必须同时省略地址和密钥字段才继承共享值。
- 独立覆盖必须同时填写 `base_url` 和 `api_key_env`；不接受只换地址，防止无意把共享 Key
  发送到另一个服务。只覆盖密钥变量同样拒绝，减少隐式组合。
- 显式独立 Key 缺失时保持空值，不退回共享 Key；共享 Key 缺失也不借用其他环境变量。
- `[connection]` 的 `base_url_env` 使用合并后的 `.env < 进程环境`，不自动搜索目标仓库配置。
- 每个 profile 仍必须有明确 model；价格、Token 和费用继续按 profile 分开计算。
- 不修改或读取本机 `.env` 的密钥内容，不创建含猜测模型 ID 的本机 `pi-claw.toml`。
- 没有全局可变 model 或客户端切换操作，不影响独立 Agent 的配置隔离。

## 验证证据

```bash
uv run --frozen pytest --cov=src --cov-report=term
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv run --frozen pi-claw eval --workers 4 --output runs/iteration-008-eval.json
uv run --frozen pi-claw eval-routing --output runs/iteration-008-routing-eval.json
uv lock --check --no-config
uv build --no-config
git diff --check
git diff --cached --check
```

- 全量测试 `312 passed`，较基线增加 29 项；总语句覆盖率 `86%`，新增连接解析模块 `100%`。
- CLI 集成测试沿正式 `CLI → load_settings → Router → OpenAICompatibleProvider` 路径运行，
  仅替换 HTTP 收发。同步与 SSE 两种模式均验证：两次请求的 URL、Authorization 一致，
  model 依次为 `small-model/large-model`，其余请求字段除工具 Schema 外一致。
- 低成本回复的写候选在升级前被丢弃，未生成目标文件；两次回复用量都计入，按各模型报价
  得到预期费用 `0.00084`。此值仅为假设单价下的离线算术，不是服务商真实账单。
- 缺失 Key、半份连接覆盖、原值含敏感字段的 URL、非法端口、畸形配置均被离线拒绝；
  假密钥未进入 CLI 输出、repr 或 Trace。
- 工程 Eval `8/8`，最大并发 `4`、隔离违规 `0`；路由合约 `6/6`。
- Ruff、编译、锁检查、文档本地链接、中文接口说明和 diff 检查通过。
- wheel/sdist 构建通过，wheel 包含 `config/connections.py`；安装包不含本机 `.env` 或
  `pi-claw.toml`。未修改依赖版本，原先 uv.lock 镜像地址改动继续保留。
- 未调用真实模型 API；网关是否提供指定 model、Key 是否有访问权限和真实费用仍待用户配置后验证。

## Git 状态

本轮未新增暂存、commit、合并或 push，保留此前 README 大小写重命名的暂存记录。
代码和文档继续留在 `refactor/package-layout` 等待用户审核。
