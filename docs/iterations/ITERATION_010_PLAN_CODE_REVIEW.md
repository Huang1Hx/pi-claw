# Iteration 010：学校模型配置与自动规划 / 编码 / 审查

## 基线、目标与状态

日期：2026-09-14。分支：`refactor/package-layout`。状态：`待用户审核`。
基线：Iteration 009，345 项测试，按复杂度选择 economy/primary，共用 API Key 和 URL。

本轮目标是在用户已配置 `.env` 的前提下，填写 model ID，实现真实的顺序规划→编码→审查
与有界返工，而不只是配置三个未被调用的 profile。保留简单任务轻量路径，未知价格不参与决策。
不更改已填写的 Key/URL、依赖锁、目录布局或旧审批机制；未调用真实模型；未 commit、merge
或 push。收尾时移除了 `.env` 中一个空的重复 Key 模板行，详见下文。

## 文件修改清单

| 文件 | 本轮修改与用途 |
|---|---|
| `pi-claw.toml`（新增，本机忽略） | 写入共享环境变量引用、学校 model ID、三阶段映射及预算；文件权限 0600 |
| `.env`（本机忽略） | 仅删除原第 3 行空的 `OPENAI_API_KEY=`，保留原第 12 行已填 Key 和现有 URL，修复重复变量阻止启动 |
| `pi-claw.ustc.example.toml`（新增） | 可公开的无密钥学校模板，不替换原通用模板 |
| `src/config/models.py`、`__init__.py` | 新增 WorkflowSettings；profile 支持推理开关格式、输出上限和 HTTP 超时 |
| `src/config/loader.py` | 校验 workflow 表、角色引用、请求/轮数/上下文边界及每模型参数；旧配置默认关闭工作流 |
| `src/routing/workflow_protocol.py`（新增） | 中文角色提示、固定 JSON 产物、重复字段/矛盾结论拒绝、跨阶段有界证据预览 |
| `src/routing/workflow.py`（新增） | 顺序状态机、真实工具证据收集、写后验证、审查重读、有界返工、终态摘要 |
| `src/routing/router.py` | 复杂分支接入协调器；共享请求/重试/Token 入口，按 purpose 计数；工具结果和宿主终态回调 |
| `src/core/schemas.py` | ModelReply 支持私有 reasoning_content、来源 profile 及标准 to_message；to_dict 不输出原始推理 |
| `src/providers/openai_compatible.py` | 三种受控推理开关字段；请求中移除内部标记，跨 profile 不传递私有推理 |
| `src/providers/streaming.py` | 同步/SSE 解析 reasoning_content；思考正文不作为文本 delta；部分思考流中断不重试 |
| `src/providers/base.py`、`__init__.py` | 新增 WorkflowStopped，区分未通过工作流和网络失败 |
| `src/core/agent.py` | 结构化产物持久化、真实 ToolResult 反馈、宿主终态通知；重复检测按阶段隔离；工作流拒绝返回 unverified |
| `src/interfaces/repl.py` | 展示规划/编码/审查阶段与审查结论，不展示原始思考 |
| `tests/test_workflow.py`（新增） | 26 项真实工具链、返工、权限、证据、预算、状态和协议回归 |
| `tests/test_workflow_transport.py`（新增） | 22 项学校配置、真实 CLI/Session + HTTP/SSE mock、推理续接和异常流验证 |
| `docs/WORKFLOW_PIPELINE.md`（新增） | 模型分配、启动方式、真实消息流、参数、用量、权限与恢复边界 |
| `README.MD`、配置/架构/工作流/Runtime/安全/范围/评测文档 | 链接和说明实际新增功能，保留旧两档路由的兼容行为 |
| `docs/iterations/README.md`、本说明书 | 迭代索引、可复现验证、风险和待审核状态 |

新增源码模块、函数、类和关键分支包含中文说明；无需新 Python 依赖。

## 配置与行为

实际模型分配：economy=`qwen3.8-chat`，planner=`qwen3.7-plus`，
primary=`deepseek-flash`，reviewer=`glm-5.3-flash`。
`qwen3.8-reasoner`、`deepseek-v4-flash-ascend1` 保留为显式手动备选，正常流程不会额外调用。
ID 来自学校公开目录的查询，不代表已验证用户 Key 权限、工具能力或强弱排名。

本机 TOML 已写入，启动方式：

```bash
cd /ABS/PATH/pi-claw
uv run --frozen pi-claw chat --config pi-claw.toml --route auto --max-steps 24
```

该命令会真实调用模型；本轮只用 mock 验证这条 CLI 路径。`.env` 仍保管 Key/URL，
路由不用 `OPENAI_MODEL`，不用填写价格。默认不启用 `--yes`，保留逐次人工审批。

自动流程的关键约束：

1. 分类只返回 simple/complex，不调用工具；简单请求保留旧轻量路径。
2. 规划只读，产出有步骤和验证方法的严格 JSON；畸形产物反馈重试，仍占阶段及总预算。
3. 编码工具全部经 Agent/Hook/Policy/Approval；只有真实成功写入后的真实成功检查才满足
   最低完成证据。没有实际写入的解释性回答不要求伪造写后验证。
4. 审查不执行命令、不写入；必须在当前审查轮用 read_file 完整重读所有实际改动文件。
5. 否决带具体 issues 回编码；新写入后旧检查失效，需要重新验证和审查。
6. approve 只是候选，外层宿主仍有否决权。只有宿主接受才是 completed。
7. 总逻辑请求最多 36；规划最多 4 次、每轮审查最多 4 次、审查最多 3 轮。阶段切换不增加
   免费额度。模型失败、预算耗尽、证据不全都不能宣称完成。
8. 只在同阶段私有对话保存推理以续接工具；跨阶段传递结构化产物和不可信历史预览。
   所有工具结果仍写公共 Session；阶段私有 reasoning_content 不落盘、不展示。

## 可复现离线验证

在项目根目录运行，下列命令不调用模型 API：

```bash
uv run --frozen pytest --cov=src --cov-report=term-missing \
  --cov-report=json:runs/iteration-010-coverage.json --junitxml=runs/iteration-010-tests.xml
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv run --frozen pi-claw eval --workers 4 \
  --output runs/iteration-010-offline-eval.json --traces-dir runs/iteration-010-offline-traces
uv run --frozen pi-claw eval-routing --output runs/iteration-010-routing-eval.json
uv lock --check --offline --no-config
uv build --offline --out-dir runs/iteration-010-dist
git diff --check
git diff --cached --check
```

实测结果：

- 全量 `393 passed`，比基线新增 48 项。总语句覆盖率 87%，workflow 96%，协议模块 98%。
- Ruff、格式、compileall、锁文件和 diff 检查通过。
- 本机配置离线加载检查发现 `.env` 同一变量被定义两次，第一处为空、第二处已填写。
  删除空模板行后，真实本机 Settings 加载成功；仅检查 Key 已存在、URL 合法、四个角色共用
  连接和 ID 映射，没有打印凭据或发送 HTTP 请求。加载器仍拒绝重复变量，不降低校验标准。
- 原工程 Eval 8/8：最大本机并发 4、隔离违规 0；原路由 Eval 6/6。
- 新增完整写入流程：真实临时文件从 alpha 变为 Alpha，运行 unittest，通过审查模型真实
  read_file 重读；共 8 个 mock 响应：分类 1、规划 2、编码 3、审查 2，合计 800 mock Token。
- 新增返工流程：第一次审查否决→改为 ALPHA→过早完成被宿主拒绝→重新测试→再次读取批准；
  共 13 个 mock 响应、1300 mock Token、2 轮审查。
- 固定每响应 80 输入 + 20 输出 Token 的目的只是核对统计，无真实模型推理和费用数据。
- 规划/审查越权写入均被拦截；未读或截断读取不能批准；新写入后的旧检查不能冒充验证；
  审查轮数耗尽和外层否决都不产生 completed。
- 不同角色各读同一文件不会误判为循环，同一阶段三次重复无进展仍停止。
- 两种真实 CLI 联调模式：同步和编码 SSE，使用真实 Session/Trace、假 Key 与 urlopen 替身；
  验证四个 model ID、共用 Authorization/endpoint、模型输出/超时/推理参数、同模型推理续接。
  公共 Session、Trace 和终端不包含阶段私有推理或假 Key。
- wheel/sdist 离线构建通过；项目目录外的已安装 wheel 导入协调器、解析学校配置和产物成功，
  路由回归仍为 6/6。构建产物和测试报告位于被忽略的 `runs/`。

例如在项目目录外复核安装包入口（将路径换为本机绝对路径）：

```bash
uv run --offline --no-project --isolated \
  --with /ABS/PATH/pi-claw/runs/iteration-010-dist/pi_claw-0.1.0-py3-none-any.whl \
  pi-claw eval-routing --output /ABS/PATH/pi-claw/runs/iteration-010-wheel-routing-eval.json
```

48/48 是流程、权限和协议断言通过率，不是学校模型任务成功率；800/1300 是固定替身用量，
不能作为真实任务 Token、价格或节省率。未采集真实模型延迟、可用率、规划质量或审查召回率。

## 取舍、风险与遗留项

- 工作流是同一宿主的顺序协调，不改变会话锁为分布式锁，不启用并行子代理或 OS 沙箱。
- 不自动加载默认 TOML；显式 `--config` 防止陌生目标仓库偷偷决定模型服务或参数。
- 不跨角色共享原始推理或工具协议状态，减少服务端推理续接歧义；只保留同阶段内存。
  当前公共历史的压缩不作用于阶段私有历史，后者超限停止，不宣称具备无限上下文。
- 规划/审查只读不能执行验证命令，因此验证由编码阶段在审批后运行、审查阶段检查证据。
- 最低完成验收不是语义正确证明，测试可能不充分；完整读取不保证模型理解或发现所有缺陷。
- 审查取证是时点读取，不提供跨进程快照锁；超过读取预算的大文件会阻止批准。
- 审查失败时已获批改动留在工作区，不自动回滚。用户可检查差异再决定下一步。
- journal 可恢复结构化产物和真实工具消息，但没有状态机精确断点续跑。下一用户回合重新
  规划，不自动恢复私有推理或重放工具。Session 文件锁仅保护提交临界区。
- 学校网关真实鉴权、工具调用、JSON 遵循、reasoning_content、thinking 参数与 SSE 兼容性
  尚未验证。当前参数是可调整的初始方案，遇到不支持的字段需按平台协议调整。
- 模型自报 Token 缺失仍记录未知；部分传输和失败请求实际消耗可能无法获知，不推算账单。

## Git 与用户配置

未新增暂存、commit、合并或 push；保留前序迭代的所有修改及原有 README 大小写重命名暂存。
`.env`、`pi-claw.toml`、本地报告均被 Git ignore；公开模板不含 Key。
简历、面试材料没有放入项目。审核后再决定提交范围与远程推送。
