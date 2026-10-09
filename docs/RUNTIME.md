# 运行时能力

## 终端输入与输出

`interfaces/terminal_input.py` 从内置命令注册表生成斜杠菜单，支持命令和 `/model` 参数补全。
菜单不调用模型、读取文件或授予权限。TTY 使用 PromptSession，管道输入保持普通 input；
审批仍由原 TerminalApprovalBroker 独立处理。Tab 单候选同步补齐，避免快速回车提交半条命令。
`/exit` 为唯一退出命令；`/model` 列出实际模型 ID 与角色映射，并可通过唯一 ID 选择已有配置。
`/status` 默认简明中文输出，`--verbose` 保留原始 JSON 供调试；这不是机器 API 的格式变更。

## 命令执行后端

`chat`/`run` 默认使用 `host` 兼容后端，只适合可信工作区；可通过配置 `[sandbox]` 或本次启动的
`--sandbox docker` 显式选择 Docker。Docker 必须先有本地 `pi-claw-sandbox:local` 镜像，启动时
解析其不可变 image ID，后续每次命令都复核 tag 未漂移。Docker 不可用、镜像缺失或参数不受支持
时直接失败，不回退 Host。

Docker 命令先以 dir-fd/no-follow 规则构造去敏只读快照，再由固定 CLI argv 启动非 root、断网、
只读 rootfs 的一次性容器。容器只挂载 `/input:ro`；entrypoint 把它复制到 256 MiB 的私有
`/workspace` tmpfs，因此测试写入不会回写原工作区。默认上限为 512 MiB、1 CPU、128 PIDs，
单条工具命令默认最多 30 秒。所有返回、异常、deadline 和取消路径都强制删除并二次确认容器，
快照也在 `finally` 清理；失败以 `cleanup_failed` 进入结果和 Trace。

Runner 的元数据只暴露数值型 `duration_seconds`、`first_output_seconds`、捕获缓冲大小，以及 image
ID、网络模式、mount 数和资源上限，不包含命令、输出、宿主路径或凭据。`first_output_seconds`
从 Docker Runner 开始计时，包含镜像复核和快照前置耗时；`duration_seconds` 还包含最终清理。
确定性攻击评测命令为：

```bash
uv run --frozen python -m evaluation.sandbox_cases \
  --output runs/stage3-sandbox-eval.json --trials 30
```

Docker 缺失时带 `docker_integration` 标记的测试会 skip；GitHub Actions 的专用任务设置强制变量，
Docker/镜像不可用时必须失败，不能把 skip 当成发布证据。

`interfaces/rendering.py` 仅记录当前显示缓冲和已展示的完成候选，精确相同正文不再重印。
编码正文已展示时，审查完成只补充审查结论；审查工具读取不会丢失去重状态。
最终回复有修订时仍展示完整新版本；无 SSE 时正常打印一次。失败状态不因去重被隐藏。
每回合重置缓冲，比较最多 100,000 字符，超限保守回到完整最终显示；会话与 Trace 不删正文。

## 流式事件与用量

`Agent(on_event=callback, streaming=True)` 订阅 `AgentEvent(type, run_id, step, seq, data)`。
seq 在一次运行内递增，不跨进程共享。同步 Provider 可继续只实现 `complete()`。
`OpenAICompatibleProvider.events(..., stream=...)` 统一发出传输事件：

| 事件 | 含义 |
|---|---|
| text_delta | 尚未验收的文本分片 |
| tool_call_delta | 工具参数片段，不能直接执行 |
| message_complete | 完整校验后的 ModelReply |
| llm_retry | 下一次请求编号与等待秒数 |
| reasoning_progress | 仅含推理分片字符数，不展示原始推理正文 |

Agent 对外还发出 run_started、model_reply、tool_call、tool_result、usage_updated、
context_pruned、context_compacted、hook_fired、hook_error、hook_blocked、loop_detected、
完成验收事件和 run_end。订阅回调收到副本，其异常被记录而不会触发工具重放。
流式分片不落 Trace，完整消息按已有机制脱敏。实时回调接收原始分片，属于可信界面接口；
终端流显示不保证对跨分片凭据做脱敏，不应要求模型输出凭据。

`ModelReply.usage` 保存服务端 prompt/completion/total Token；没有 usage 时为 None。
`RunResult.usage` 聚合完整响应，包括被验收拒绝的候选。`/usage` 分别展示当前会话和当前
项目工作区的跨会话累计值；后者已经包含前者，输入/输出/总量均来自持久化账本。
失败或中断请求的计费可能未反馈到客户端，不能将本地已知计数等同账单。
`/context` 中字符近似 Token 与实际 usage 分开。路由按复杂度选模型，默认只统计调用与 Token，
不要求价格。分类回复不进入正式会话，但完整响应的 usage 通过 `usage_observed` 汇入总量。
`intent_classified` 记录标签、来源和所选 profile，`model_routed.purpose` 区分分类/执行。
旧价格字段仅按需兼容，见 [配置与模型路由](CONFIGURATION_AND_ROUTING.md)。

## 有限重试

工作流增加 `workflow_stage` 与 `workflow_artifact`。结构化计划、编码候选和审查结果写入
Session/Trace；阶段私有 `reasoning_content` 只用于同模型续接，不进入公共工作流历史。
中间响应 usage 全部计入，`requests_by_purpose` 细分阶段。只读规划/审查不流式展示 JSON，
编码仍可流式显示候选。阶段间重复检测独立，整体调用与返工另有上限。
阶段私有上下文目前不做有损压缩，超限停止；公共上下文继续沿用以下压缩机制。
详见 [多模型工作流](WORKFLOW_PIPELINE.md)。

默认最多 3 次尝试（包含第一次）；429、500、502、503、504 和暂时性连接错误可重试。
采用指数退避，数值 Retry-After 优先，单次等待最多 10 秒。401/400、畸形 JSON、无效工具
参数不重试。SSE 已输出内容、工具或推理分片后出错立即失败，避免重复生成。
只有完整且正常结束的响应进入 Agent；该过程不会重试任何工具副作用。

chat 默认请求 `stream_options.include_usage`；不兼容该流协议的服务可使用 `--no-stream`。
本地合约测试全部使用 HTTP/SSE 替身；已授权的真实模型诊断和小示例记录见
[Iteration 011](iterations/ITERATION_011_WORKSPACE_TERMINAL.md)，不替代多任务质量评测。

## 上下文管理

先执行 `prune_tool_results()`，仅裁剪旧用户回合、近期保留窗口之外的大工具正文。
保留 tool_call_id、成功状态、失败原因和命令退出码，以及有界预览；裁剪是有损的。
默认正文预览阈值 4,000 字符，最近保留窗口默认 8 条消息。
超出总消息/字符预算时，继续使用已有确定性摘要压缩。不新增 LLM 摘要请求。
原始 journal 不变，checkpoint 保存处理后的活动窗口，恢复使用 through_seq 接上日志尾部。

## 重复调用检测

默认在最近 12 次工具观察中，相同工具名、标准化参数和结果出现 3 次即返回 loop_detected。
忽略随机 call id；支持 A/B/A/B/A 等循环。成功写入清空窗口。
这是无进展启发式，重复读取确有意义的宿主可提高 `repetition_threshold`（不超过默认窗口）。
每次 Agent.run 重新创建检测器；相同 Agent 的并发/重入调用会报错。

## 取消与显式回滚

用户发起取消（`ExecutionControl.cancel` 或 Ctrl+C）后，Agent 在每次模型请求和工具调用前检查
checkpoint，Host 命令运行器每 20ms 轮询并在取消时清理整个进程组，因此取消会尽快停止后续工具；
但已经提交的写入不会自动撤销。

宿主写工具（`write_or_replace`、`apply_patch`）每次**成功提交后**都在 `WriteJournal` 记录
`{path, kind, preimage, post_version}`，供显式回滚使用；策略拒绝、审批拒绝、版本冲突和写入异常
都不记录。`run_command` 执行代码产生的外部副作用（脚本改文件、`__pycache__`、git 操作、网络/
进程）无法回滚。

显式回滚只在用户要求时发生：

- `pi-claw run ... --rollback-on-cancel`：仅当本次运行以 `cancelled` 结束时，逆序撤销本次由宿主
  写工具提交的改动，并在 JSON 终态与标准输出打印 `{restored, deleted, skipped, failed}` 摘要。
- REPL `/undo`：撤销当前会话最近一次已提交的宿主写入，可重复执行逐条回退。

回滚是版本绑定的 fail closed：恢复/删除前复核当前文件版本与记录一致，外部改动或文件缺失一律
**跳过并上报**，绝不覆盖；底层出错计入 `failed` 并保留现场，不伪装成功。日志有界，超出条目或
字节上限后停止记录并标记 `incomplete`，摘要会提示可能未覆盖全部改动。回滚过程写入 Trace 事件
`rollback_started/rollback_restored/rollback_deleted/rollback_skipped/rollback_completed/rollback_failed`，
只含路径、动作与计数，不含文件正文或密钥。没有显式标志/命令时保持现状，与
`docs/PROJECT_SCOPE.md` 一致。

## Hooks

Hooks 仅由可信宿主显式注入，不自动加载用户仓库文件、Python 插件或 shell 命令。

```python
from core.agent import Agent
from core.hooks import HookBus, HookDecision

hooks = HookBus()

def protect_changes(call):
    """示例：宿主将当前任务限制为只读。"""
    if call["name"] in {"write_or_replace", "apply_patch"}:
        return HookDecision(False, "当前任务不允许写入")
    return HookDecision()

hooks.register("before_tool", protect_changes)
# agent = Agent(provider, executor, hooks=hooks)
```

支持 run_start/run_end、before_tool/after_tool、before_compact/after_compact。
只有 before_tool 的拒绝决定阻止操作；其异常也默认拒绝。观察 Hook 的异常记录为 hook_error，
不重跑操作。每个回调收到输入副本，不能通过修改参数绕过审批。
允许只表示继续现有 Policy/Approval，不意味着授予文件或命令权限。

## 持久化用量

SessionStore.record_usage() 以 run_id 幂等写 usage.json，共用会话文件锁和原子替换。
账本有事件数、字节上限；旧会话没有该文件时正常加载。运行与账本写入不是跨文件事务：
进程在回合结束和账本落盘之间退出可能丢失这次计数，但不会自动重放任务。

SessionStore.workspace_usage() 只读取当前真实工作区路径对应的分组，并逐会话持锁读取；
正常累计、无记录会话、不可读会话分别计数，损坏文件和符号链接不会被静默当成零。
无记录可能意味着还没调用模型或旧版本没保存，不能推断实际消费。不会读取其他工作区，
也不会按项目子目录猜测 Token 归属。没有新建全局总账，不改变原 run_id 幂等格式。
这是逐会话快照，不是跨会话事务；并发运行未落盘的最新用量需下次查询，缺失的供应商账单不补算。
