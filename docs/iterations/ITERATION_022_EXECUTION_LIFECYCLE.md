# Iteration 022：Agent 与命令生命周期

## 状态

待用户审核，尚未提交。未推送远程、未合并 `main`。本轮只执行离线测试，没有调用学校模型或
Kimi，也没有修改 78/90 冻结评测基线、评分器、隐藏用例、权限白名单或最多两轮修复约束。

## 目标

Task 3.4 解决此前 Trace 中三类容易混淆的问题：本地请求预算被误报为 Provider 故障、重复策略
拒绝持续消耗请求，以及超时/取消后宿主进程或后台子进程残留。420 秒仍是每个 trial 的父级硬
上限；实现不能靠延长时间、增加模型请求或重放副作用换取成功。

## 实现

| 范围 | 修改 |
|---|---|
| 共享控制 | 新增 `ExecutionControl`，以单调时钟截止时间和线程安全事件贯穿 Agent、路由、团队与 Runner |
| 终态 | 新增 `request_budget_exhausted`、`deadline`、`cancelled`；Provider 故障保持独立 |
| 循环诊断 | `loop_detected` 保持兼容，Trace 进一步区分 `semantic_loop` 与 `policy_stall`，记录工具和具体策略原因 |
| 模型请求 | 每次 HTTP 请求和重试前检查控制对象，单请求超时收紧到剩余时间，取消后禁止第二次尝试 |
| 团队/评测 | 主 Agent 与全部子 Agent 共享同一截止时间；父级仍在 420 秒硬终止，worker 预留 3 秒落盘和清理 |
| Host 命令 | 每次命令创建独立进程组；并发排空 stdout/stderr，内存只保留固定前缀；超时/取消 TERM 后最多等 2 秒再 KILL |
| 会话恢复 | 中断的工具调用补写 `interrupted=true, replay_allowed=false` 的 tool 观察，恢复会话不由宿主自动重放 |
| 报告 | 本地请求预算不计为 Provider 不可用；报告保存停止原因、阶段和剩余秒数 |

## 测试设计

实现前先观察到缺失模块、旧终态、无控制参数、无进程组清理和 Trace 字段缺失等失败；随后覆盖：

- 已过期 deadline 和显式 cancel 均在模型请求前停止；
- 非白名单命令和路径越权连续三次后有界停止，并返回具体拒绝原因；
- 规划—编码—审查工作流耗尽请求预算时记录 `phase=coding`；
- 主 Agent、两个只读子 Agent 共享取消信号且总请求数为零；
- 429 重试事件后取消，不发起第二次 HTTP 请求；
- stdout/stderr 各输出 20 万字符仍不死锁，内存结果保持有界；
- 普通超时、忽略 SIGTERM、父进程正常退出后遗留后台子进程和运行中取消均完成进程组清理；
- 四个 Host Runner 并发时输出互不串线；
- 中断工具写入不可重放观察，继续会话不会再次执行原命令；
- 旧评测报告仍可读取，新 `request_budget_exhausted` 不污染 Provider 可用性。

## 验证

- 完整离线回归：648 项通过。
- `ruff check src tests` 与 `ruff format --check src tests` 通过。
- wheel 和 sdist 构建通过，产物写入项目外临时目录。
- 对本轮差异执行凭据签名扫描，没有发现 Key、Bearer Token 或私有模型 URL。
- `git diff --check` 通过。

## 边界与下一步

Host 后端现在具备可靠停止和有界输出，但它仍在用户宿主机执行仓库代码，不是 OS 沙箱。下一步
Task 3.5 必须先建立不含 `.env`、`.git`、会话、Trace、symlink、FIFO 和设备文件的临时快照；
Task 3.6 才能把该快照只读挂载到 Docker。Docker 不可用时必须 fail closed，不能回退 Host。
