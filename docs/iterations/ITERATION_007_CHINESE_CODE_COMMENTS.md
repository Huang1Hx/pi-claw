# Iteration 007：中文函数说明、接口契约和关键逻辑注释

## 状态与范围

日期：2026-09-11。状态：`待用户审核`。分支：`refactor/package-layout`。
基于 Iteration 006 的目录布局和 283 项通过测试，按用户要求补充源码中文注释。
不修改业务行为、配置、工具协议、会话格式或模型提示词，不新增依赖。

本轮更新 40 个 Python 源文件：新增 113 处原先缺失的函数/方法文档字符串，
扩写核心接口契约，并在易误解的关键分支添加解释“为什么”的行内注释。
扫描全部 63 个源码模块：62 个类和 262 个函数/方法均有中文文档字符串，
包括构造方法、私有辅助方法和局部函数。已有准确说明保留，不机械重述每行语法。

## 修改位置

以下路径均相对于 `src/`。

| 模块 | 修改文件 | 注释重点 |
|---|---|---|
| 核心编排 | `core/agent.py` | run 输入输出、共享活动历史、消息持久化回调、工具 ID 配对、完成候选验收、流结束检查 |
| 上下文与验收 | `core/context.py`、`core/completion.py` | 活动窗口与原始日志分离、确定性摘要、字符估算限制、最后写入之后的验证证据 |
| 核心协议与辅助 | `core/hooks.py`、`core/loop_detection.py`、`core/prompts.py`、`core/schemas.py`、`core/usage.py` | Hook 数据与权限边界、重复调用窗口、入口发现、消息互斥校验、已知/未知用量 |
| 模型适配 | `providers/base.py`、`providers/openai_compatible.py`、`providers/streaming.py`、`providers/scripted.py` | Provider 输入输出契约、同步/可选流式接口、有限重试、完整 SSE 校验、固定脚本不做模型推理 |
| 工具协议与执行 | `tools/registry.py`、`tools/builtins.py`、`tools/executor.py` | Schema 不是权限、注册与策略分离、handler 调用方式、布尔审批接口、执行错误归一化 |
| 文件和命令工具 | `tools/write.py`、`tools/edit.py`、`tools/files.py`、`tools/grep.py`、`tools/preview.py`、`tools/shell.py` | 创建/唯一替换、多段内存校验、指纹复核、扫描预算、预览截断、argv 与环境白名单 |
| 安全 | `security/policy.py`、`security/approval.py`、`security/workspace.py` | 安全路径和参数、只读/测试命令白名单、一次/会话批准、敏感文件、兼容导出 |
| 会话门面 | `persistence/session_store.py`、`persistence/models.py` | 工作区路径摘要、会话 ID、元数据校验、文件权限、密钥不进状态 |
| 持久化一致性 | `persistence/journal.py`、`persistence/checkpoint.py`、`persistence/locking.py`、`persistence/storage.py` | 排他锁与序号职责、日志先落盘、快照尾部追赶、未完成工具恢复、原子文件替换 |
| 轨迹和用量 | `persistence/trace.py`、`persistence/usage.py` | 实例线程锁范围、脱敏后截断、纯只读回放、run_id 幂等计数 |
| CLI 与交互 | `interfaces/cli.py`、`interfaces/repl.py`、`commands/registry.py` | 入口装配、退出码、命令处理函数约定、宿主命令不进入 LLM、快照序号提交 |
| 配置与成本 | `config/loader.py`、`routing/router.py`、`routing/cost.py` | 配置优先级、密钥按 profile 配对、低成本 Schema 与候选双重检查、升级、请求和金额预算 |
| 离线评测 | `evaluation/runner.py`、`evaluation/routing.py` | 临时工作区隔离、线程计数锁、固定顺序汇总、宿主断言、假设 Token 与单价 |

## 特别澄清的实现边界

- `Agent._running` 是重入保护标记，不是线程互斥锁；并发任务要用独立 Agent。
- Session 文件锁只串行化同一会话的本地读改写；`expected_seq` 再拒绝旧快照提交。
  锁不负责授权，也不是跨主机分布式锁。
- TraceWriter 的锁只保护同一写入器实例，不宣称多个进程/写入器互斥。
- 写工具的指纹检查与文件替换不是原子比较交换，不宣称提供跨进程编辑事务。
- 原子替换防止半文件写入，不意味着 journal、state、checkpoint 是跨文件原子事务。
- 流式文本/参数是候选片段，必须等结束标志、finish_reason 和参数 JSON 校验通过。
- 注册工具、提供 Schema、Hook 返回允许都不绕过 Policy 和 Approval。
- 上下文字符近似不保证中文/代码 Token 上界，不能拿来替代服务端 usage 或费用。
- 金额预算约束下一次请求，不能保证本次账单不越额；失败或缺失 usage 的费用保持未知。
- 子进程输出在捕获后截断，不是流式内存上限；argv 白名单和环境裁剪不是 OS 沙箱。
- 完成验收只要求最后一次写入之后至少有一项受认可的成功检查，不声称证明所有业务需求。

## 验证

1. 修改前记录全部 63 个源文件的语法树摘要。
2. 修改后去掉模块、类、函数文档字符串，并忽略行列号，再比较全部语法树：差异为 0。
   这确认除注释和 `__doc__` 外，没有改动可执行语句、常量、签名、导入或控制流。
3. 扫描所有源码模块、类、函数：均有中文文档字符串，没有遗漏的函数说明。

```bash
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen python -m compileall -q src
uv run --frozen pytest
uv run --frozen pi-claw eval --workers 4 --output runs/iteration-007-eval.json
uv run --frozen pi-claw eval-routing --output runs/iteration-007-routing-eval.json
git diff --check
git diff --cached --check
```

- 全量测试：`283 passed`。
- 工程离线评测：`8/8`；最大并发 `4`，隔离违规 `0`。
- 路由离线合约：`6/6`；只验证固定替身，不调用真实模型 API。
- Ruff、编译和 diff 空白检查通过。
- 本轮不增加测试用例，因为可执行语法树无变化；复用现有完整回归验证行为。

## Git 状态

本轮没有新暂存、commit、合并或 push。保留之前 README 大小写重命名的暂存状态，
以及已有的目录重构和 uv.lock 修改。所有改动仍待用户审核。
