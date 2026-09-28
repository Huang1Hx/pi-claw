# Iteration 005：工具目录、命令配置与成本路由

## 状态与基线

`待用户审核`，分支 `refactor/package-layout`。在 Iteration 004 未提交工作区上继续完善。
此前 233 项测试通过，但工具实现仍集中于 execution，交互命令使用 REPL 长分支，
缺少 `.env` 加载和模型路由。当前迭代按用户反馈细化职责。

## 修改清单

| 范围 | 文件 | 变化 |
|---|---|---|
| 工具 | `tools/read.py`、`write.py`、`edit.py`、`grep.py`、`shell.py` | 从 execution 拆出具体工具，实现读取/创建/替换/多段编辑/搜索/受控命令 |
| 工具公共层 | `tools/executor.py`、`registry.py`、`builtins.py`、`files.py`、`preview.py` | 统一注册、授权、预览、原子文件写入；保持旧工具协议名 |
| 命令 | `commands/registry.py`、`help.py`、`status.py`、`model.py`、`exit.py`、`sessions.py`、`context.py` | 12 个显式注册命令，别名冲突检查，帮助自动生成，命令不进入模型 |
| 配置 | `config/models.py`、`env.py`、`loader.py` | 类型化 profile、64 KiB 有界配置读取、环境优先级、密钥引用和参数校验 |
| 模板 | `.env.example`、`pi-claw.example.toml`、`.gitignore` | 供应商空密钥模板、模型/报价/预算模板；本机 .env/配置忽略跟踪 |
| 本机配置 | `.env` | 已创建空密钥文件，权限 0600；不进入 Git 或安装包 |
| 路由 | `routing/intent.py`、`router.py`、`cost.py` | 简单只读任务选 economy；复杂/含糊任务选 primary；失败/副作用/轮数触发升级；分模型费用 |
| Runtime | `core/agent.py`、`schemas.py` | 重置每回合路由预算，聚合被丢弃候选 usage，RunResult.routing 和路由 Trace |
| Provider | `providers/openai_compatible.py` | 可选 max_tokens 输出限制；显式空 key 不回退到其他供应商的环境 key |
| 界面 | `interfaces/cli.py`、`repl.py` | --config/--route/--env-file/--no-env，路由显示、状态查询、切换模型新建 Session |
| 评测 | `evaluation/routing.py` | auto 与固定 primary 的六场景离线对照，逐运行 Trace 和 JSON 报告 |
| 测试 | `test_config.py`、`test_routing.py`、`test_commands.py`、`test_cli_config.py`、`test_routing_eval.py` | 配置安全与优先级、真实调用链、路由/预算、会话隔离和费用算术 |
| 文档 | README、ARCHITECTURE、RUNTIME、PROJECT_SCOPE、SECURITY、EVALUATION、API_KEY_GUIDE、WORKFLOW_AND_DATA_FLOW | 当前目录、配置入口与路由边界同步 |
| 专题文档 | `CONFIGURATION_AND_ROUTING.md` | 配置步骤、工具/命令映射、完整路由状态与成本语义 |

## 目录清理

`src/`、`tests/`、`docs/` 原本已在项目根目录，继续保持。确认源码、测试和构建不引用根目录
`tmp/`、`fixtures/` 后，将其移出工作区并保留在 Git 仓库外的
`/Users/huanghx/project/pi-claw-materials/archive/`：

- `iteration-005-tmp/`：旧 PDF 渲染和 Kimi CLI 参考克隆，约 31 MB；
- `iteration-005-fixtures/`：空 sample_repo 与 macOS 系统文件；
- `iteration-005-execution-cache/`：工具搬迁后残留的 Python 缓存。

上述内容都未被当前 Git 跟踪；保留原文件以便恢复。测试 fixture 继续由 pytest tmp_path
动态创建，不依赖根目录 fixtures 文件夹。

## 核心行为与取舍

- 自动路由仅用本地规则，不额外调用分类模型；它不是泛化语义意图识别器。
- 低成本只读限制既作用于工具 Schema，也校验完整回复；任何副作用候选在工具执行前升级。
- 丢弃候选仍消耗 Token，其 usage 和成本计入同一回合。
- 固定 profile 不升级；模型选择不改变 Policy/Approval 的授权规则。
- `.env` 不执行 shell、不展开变量、不修改全局环境；只从启动目录或指定路径加载。
- `/model` 切换建立新 Session，不把旧模型上下文和持久审批自动迁移到新服务。
- 费用以配置单价乘服务端 usage；缺失报价/usage 显式记录未知。
- 金额预算是停止下一请求的阈值，不承诺本次服务端账单绝不越额。
- 相比 Pichu 的目录职责，Pi-Claw 仍不自动加载仓库自定义命令或使用配置全局单例。
- 原会话 journal/checkpoint 格式和六个模型工具协议名保持不变。

## 验证记录

```bash
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen python -m compileall -q src/pi_claw
uv run --frozen pytest --cov=pi_claw --cov-report=term-missing
uv run --frozen pi-claw eval --workers 4 --output runs/iteration-005-eval.json
uv run --frozen pi-claw eval-routing --output runs/iteration-005-routing-eval.json
uv lock --check --no-config
uv build --no-config
git diff --check
```

- 全量测试 `278 passed`，较 Iteration 004 新增 45 项用例；总语句覆盖率 `86%`。
- 原工程 Eval `8/8`，安全场景 `100%`，并发 worker `4/4`、最大并发 `4`、隔离违规 `0`。
- 路由合约 `6/6`，auto 与全 primary 共 12 个隔离运行；副作用候选没有执行。
- 固定替身和假设单价下：auto 14 次请求、2 次升级、费用 `0.00644`；全 primary 12 次请求、
  费用 `0.0084`；费用算术差异 `23.3%`。auto 请求次数更多但单价更低，这不是实际模型节省率。
- 每次替身固定 100 input + 20 output Token，假设 economy 单价 1/2、primary 5/10
  CNY/百万 Token；本地 P95 记录在报告中，不冒充真实 LLM 延迟。
- Ruff、编译、锁检查和 diff 空白检查通过。
- wheel/sdist 构建成功；wheel 包含新工具/命令/配置/路由与 Eval 模块，sdist 包含两个配置
  模板；两者不包含真实 `.env`，也不包含被清理的 tmp/fixtures。
- wheel 安装到源码目录外全新 uv 环境后，原 Eval 再次 `8/8`、路由合约再次 `6/6`，CLI
  模块入口和新参数帮助可用。
- 检查 64 个 Python 模块均有中文模块说明，顶层公共类/函数均有 docstring；文档本地链接有效。
- `.env` 被 Git 忽略，`.env.example` 可跟踪；未调用真实 API。

## 待配置与实际边界

用户需要填写真实 API key、模型 ID、配套端点及报价后，才能进行真实成本/质量实验。
路由合约评测的价格和 Token 都是固定假设，不能直接作为真实节省率。原有本机锁、工作区隔离
和并发 Eval 仍不等于分布式调度或 OS Sandbox。

本轮没有 Git commit/push/merge；已有的 `uv.lock` 镜像地址差异继续保留。
