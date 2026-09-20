# Iteration 009：轻量模型分类与复杂度分流

## 状态与基线

日期：2026-09-14。状态：`待用户审核`。分支：`refactor/package-layout`。
基线为 Iteration 008 的 312 项测试与共享 API Key/URL 连接。

用户明确：复杂任务交能力强的模型，简单判断交轻量模型，例如意图识别后选择分支；
模型价格未知，不需要基于价格计算路由。本轮不读取或修改用户本机 `.env`，不调用真实模型，
不改变项目目录布局、工具权限链或 Session 格式。

## 实现行为

新模板启用 `routing.intent_strategy = "model"`。每个用户回合第一次模型请求前：

1. economy 收到固定分类提示和当前任务，不附带文件、工具 Schema 或完整会话。
2. 只接受 `{"complexity":"simple"}` 或 `{"complexity":"complex"}`。
3. 简单意图识别、给定分支选择、短文本提取、简单解释或单步只读任务交 economy 执行。
4. 代码修改、调试、多步骤推理、架构等复杂任务交 primary 执行。明显副作用和部分缺失上下文
   表述还有宿主规则兜底；分类失败也转 primary。
5. 后续执行保留原失败升级：工具失败、完成验收拒绝、轻量配额耗尽或越界工具候选转主力。

两个角色仍复用 `[connection]` 的地址与 Key，通过请求 `model` 字段选择服务端模型。
角色能力由用户指定，系统不查询价格、不比较报价，也不自动给模型能力排名。
分类结果不直接执行任务，不成为正式会话中的 assistant 消息。

## 修改清单

| 文件 | 本轮修改 |
|---|---|
| `src/routing/classifier.py`（新增） | 中文分类提示、4000 字符输入上限、两标签 JSON 协议、重复/额外字段拒绝、宿主保守检查 |
| `src/routing/router.py` | 每回合延迟分类一次；统一分类/执行请求预算与 HTTP 工厂；分类无工具、不透传 delta；异常 fallback；统一关闭底层迭代器；按 purpose 记录请求 |
| `src/routing/cost.py` | 未配置价格时只输出按模型的请求与 Token，不产生金额或“费用未知”字段；保留显式旧金额配置兼容 |
| `src/config/models.py`、`loader.py` | 增加并校验 `intent_strategy`；旧配置默认 `rules`，显式 `model` 开启轻量分类 |
| `src/core/agent.py` | 持久化 `intent_classified`；复用已有 `usage_observed` 将分类响应计入总 usage |
| `src/interfaces/repl.py` | 展示复杂度标签和执行分支，不显示分类原文 |
| `src/evaluation/routing.py` | 路由 suite 升为 v2；共享连接、预设分类替身，不设价格；报告分类/总请求、主力执行次数和 Token |
| `src/interfaces/cli.py` | 更新 eval-routing 帮助及结果，移除假设费用节省率 |
| `pi-claw.example.toml` | 启用模型分类、去掉价格模板；保留用户已选轻量模型 ID，主力 ID 留待用户填写 |
| `tests/test_model_intent_routing.py`（新增） | 33 项复杂度分类、失败处理、工具隔离、记账、配额、共享连接与流式协议测试 |
| `tests/test_shared_model_connection.py` | 模板测试只修改副本中的模型 ID，不依赖用户仍保留旧占位符；验证新策略无需价格 |
| `tests/test_routing_eval.py` | 改为 v2 分流/调用分布断言，不再断言虚构价格节省比例 |
| `README.MD`、`docs/CONFIGURATION_AND_ROUTING.md` | 新配置、复杂度路由、调用开销、旧配置兼容及启动步骤 |
| `docs/ARCHITECTURE.md`、`WORKFLOW_AND_DATA_FLOW.md`、`RUNTIME.md` | 分类与执行的消息、事件和用量路径 |
| `docs/EVALUATION.md`、`PROJECT_SCOPE.md`、`SECURITY.md` | v2 指标、未验证边界和分类无工具权限说明 |
| `docs/iterations/README.md`、本说明书 | 新增本次迭代索引和可复现证据，旧迭代保留历史描述 |

## 安全、预算和兼容取舍

- 分类请求不附带工具；即使回复夹带工具候选，也不交 ToolExecutor，不进入 Session。
- 分类 JSON 不能指定任意模型、端点或执行指令。未知标签、额外字段、重复字段、Markdown、
  非文本等都判失败；服务错误正文不写入分类事件，只记录固定原因。
- 分类最多输出 `min(128, max_output_tokens)` Token；超过 4000 字符的任务跳过分类、交主力。
  输入不截断后强行猜简单，执行阶段仍接收原任务。
- 分类尝试属于 `max_requests`，但不占 `max_cheap_requests` 的轻量执行配额。
  底层 HTTP 重试沿用 max_attempts 和 llm_retry，逻辑请求数不等同 HTTP 尝试数。
- `classification_failures` 包含协议/网络失败与过长跳过；完整分类响应的 usage 会计入，
  未收到响应的服务端 Token 无法知道，不虚构为零。按模型 ledger 只汇总完整响应。
- 固定 `--route <profile>`、两个角色引用同一 profile 时均不分类；下一用户回合重置状态。
- `intent_strategy="rules"` 或旧配置省略该项时沿用原本地规则，不强加新的网络调用。
- 历史主动配置价格/金额预算的用法仍兼容，但价格永不参与复杂度判断或模型选择。
  新模板不含价格；用户不需要补报价。
- 轻量模型分类不是安全判定器。任何模型的工具候选仍经 Policy/Approval，未引入 OS 沙箱。
- 分类每回合增加一次请求；同一 profile 作为分类和轻量执行模型，不另建第三种模型角色。
  本轮只做任务级分流，没有实现复杂任务内部任意子步骤的动态路由或训练过的分类器。

## 可复现验证

在项目根目录执行，不调用真实模型 API：

```bash
uv run --frozen pytest --cov=src --cov-report=term-missing
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv run --frozen pi-claw eval-routing --output runs/iteration-009-routing-eval.json
uv run --frozen pi-claw eval --workers 4 \
  --output runs/iteration-009-offline-eval.json \
  --traces-dir runs/iteration-009-offline-traces
uv lock --check --offline --no-config
uv build --offline --out-dir runs/iteration-009-dist
git diff --check
git diff --cached --check
```

- 全量 `345 passed`，新增 33 项；总语句覆盖率 86%，分类模块 100%，Router 91%。
- Ruff、格式、编译、依赖锁、diff 校验通过；中文函数/类说明和当前文档本地链接检查通过。
- 工程 Eval：8/8，最大并发 4，隔离违规 0；路由合约：6/6，副作用执行 0。
- HTTP mock 验证分类和执行使用相同 URL/Authorization、不同 model；分类无 tools，不流式，
  输出上限 128；执行保持 2048 上限并支持同步/SSE。测试不依赖本机密钥。
- wheel/sdist 离线构建通过，wheel 含 classifier 与内置 Eval 数据，不含 `.env/pi-claw.toml`。
- 在项目目录外，通过 `uv run --offline --no-project --isolated --with <本轮 wheel> pi-claw`
  启动已构建安装包，路由合约同样为 6/6；报告位于 `runs/iteration-009-wheel-routing-eval.json`。

### v2 固定替身对照

| 指标（6 个场景合计） | 轻量分类 + 自动分流 | 固定 primary |
|---|---:|---:|
| 分类逻辑请求 | 6 | 0 |
| 总逻辑请求 | 20 | 12 |
| 主力执行请求 | 8 | 12 |
| 升级次数 | 2 | 0 |
| 固定替身返回的总 Token | 2010 | 1440 |

分类替身回复 50 输入 + 5 输出 Token；执行替身回复 100 输入 + 20 输出 Token。
分类标签事先设定，6/6 是路由合约与宿主执行断言，不是真实分类准确率。
主力请求减少不意味着总 Token 更少、质量更高或一定省钱，报告不含单价或费用节省率。

## 尚待用户配置和真实验证

- 在本机 `pi-claw.toml` 填写同一服务可访问的主力模型 ID。保留了模板中的既有轻量 ID，
  但没有联网确认服务是否提供该 ID；不代替用户猜模型名称。
- `.env` 的 Key/URL 保持不动；路由 model ID 来自 TOML，不来自单模型 `OPENAI_MODEL`。
- 用户授权后再验证网关兼容性、分类 JSON 遵循率、真实任务成功率、复杂任务误降级率、
  分类开销、分模型 Token、延迟和升级率。未知价格无需补填，也不用于推算账单。

## Git 状态

未新增暂存、commit、合并或 push；仍在 `refactor/package-layout`。
保留已有 README 大小写重命名暂存记录及前序未提交改动，不修改原 uv.lock 的镜像差异。
本机 `.env`、`pi-claw.toml` 和 `runs/` 经 Git ignore 校验仍被忽略。等待用户审核。
