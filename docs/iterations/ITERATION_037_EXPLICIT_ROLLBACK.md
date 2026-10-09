# Iteration 037：取消后的显式事务回滚

## 状态与边界

状态：实现与离线验证已完成；等待推送 `optimize` 并合并 `main`。

本轮回应用户“取消后立即停止工具调用并回滚事务”的诉求。取消本身早已生效（Agent 在每次模型请求
和工具调用前 `checkpoint`，Host 命令运行器每 20ms 轮询并清理整个进程组），缺少的是写入撤销。
经与用户确认，采用**显式回滚**：默认不静默撤销，只有用户显式请求（CLI `--rollback-on-cancel` 或
REPL `/undo`）才回滚本次由宿主写工具产生的改动。

回滚边界：只覆盖宿主追踪的 `write_or_replace`/`apply_patch` 单文件提交，`run_command` 执行代码
产生的外部副作用（脚本改文件、`__pycache__`、git 操作、网络/进程）无法回滚，也不承诺跨文件
ACID 事务。

## 动机

此前文件工具是单文件、版本绑定的原子写，但没有事务/撤销能力；`docs/PROJECT_SCOPE.md` 明确记录
“没有事务回滚”。用户在被取消后只能手动检查并还原，或任由半途改动留在工作区。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/journal.py` | 新增 `WriteRecord`/`WriteJournal`/`RollbackSummary`/`RollbackOutcome`；有界记录成功提交，逆序回滚并逐路径跟踪期望版本 |
| `src/tools/files.py` | `_record_write`：提交成功后读取 `post_version` 并记录；未配置日志时静默跳过 |
| `src/tools/write.py`、`src/tools/edit.py` | 创建/替换/补丁在成功提交后写入日志，携带 `preimage` |
| `src/tools/executor.py` | 每个执行器持有 `WriteJournal(max_entries=64, max_bytes=max_write_bytes)` |
| `src/security/secure_fs.py` | 新增 `atomic_delete_file(relative, expected=version, max_bytes)`：dir-fd、no-follow、版本校验删除 |
| `src/core/agent.py` | `rollback`/`rollback_run`/`undo_last`，写入 `rollback_*` Trace 事件 |
| `src/core/schemas.py` | `RunResult.rollback` 摘要字段 |
| `src/interfaces/cli.py` | `run --rollback-on-cancel`：仅在 `cancelled` 终态回滚并打印摘要 |
| `src/commands/undo.py`、`src/commands/__init__.py` | REPL `/undo`：逐条撤销最近一次宿主写入 |
| `tests/test_write_journal.py`、`tests/test_rollback.py` | 记录、版本校验删除、逆序回放、跳过/失败与显式触发回归 |
| `docs/RUNTIME.md`、`docs/PROJECT_SCOPE.md`、`docs/SECURITY.md` | 边界说明 |

关键设计：

- **只记成功提交**：策略拒绝、审批拒绝、版本冲突、写入异常都不进入日志，避免把未发生的改动当成
  可回滚目标。
- **版本绑定 fail closed**：回滚恢复/删除前复核当前文件版本与记录一致；外部改动或缺失一律跳过
  并上报，绝不覆盖。
- **逐路径期望版本**：同一路径多次写入时，回滚后续提交会产生新 inode，不能直接复用更早记录的
  `post_version`；每次成功回放后重读该路径版本，作为下一条更早记录的期望版本。
- **有界日志**：条目数 1–1024（默认 64）与单条字节上限；超限停止记录并标记 `incomplete`，摘要
  提示可能未覆盖全部改动，避免内存无界增长。
- **不伪装成功**：底层失败计入 `failed` 并保留现场；`blocked` 路径的后续更早记录跳过，避免在
  未知状态下继续覆盖。
- **摘要不含正文**：`RunResult.rollback` 与 Trace 只含路径、动作与计数，不泄漏文件正文或密钥。

## 验证

- `tests/test_write_journal.py`（6 项）与 `tests/test_rollback.py`（9 项）覆盖记录边界、创建删除、
  替换还原、同路径多次替换逆序回放、外部改动跳过、底层失败上报、`last_only` 逐条撤销与 Agent
  Trace 事件。
- `tests/test_terminal_experience.py` 新增 `/undo` 与 `--rollback-on-cancel`（cancelled 回滚 /
  completed 不回滚）回归。
- 完整 `pytest`（沙箱权限用例在本地沙箱内 deselect，CI 全量运行）、Ruff、format、compileall 通过。
