# Iteration 033：VerificationPlan 接入 CLI/REPL/工作流

## 状态与边界

状态：实现与离线验证已完成；未调用真实模型。

本轮为阶段 4 Task 4.2：把 Iteration 032 的 `VerificationPlan`/`PlannedCompletionVerifier` 接入
交互入口与多模型工作流，给出保守默认计划（写入后必跑测试，静态检查可选）。它不改变验收器只读
证据、不执行命令的边界，也不引入受保护模式或变更—测试映射（那需要宿主按工作区显式声明）。

## 动机

Iteration 032 只提供了机制，默认调用点仍用旧的 `ToolEvidenceCompletionVerifier`，因此“写入后只跑
lint 就宣称完成”在真实入口仍被接受。本轮让默认入口执行必跑类别检查，同时避免误伤合法的测试
创建。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/completion.py` | 新增 `DEFAULT_VERIFICATION_PLAN`（必跑 test、可选 lint/compile/lock）与 `default_completion_verifier()` |
| `src/interfaces/cli.py` | `demo` 与 `run` 使用 `default_completion_verifier()` |
| `src/interfaces/repl.py` | 交互回合使用 `default_completion_verifier()` |
| `src/routing/workflow.py` | 编码阶段验收改用 `default_completion_verifier()` |
| `tests/test_verification_plan.py`、`tests/test_workflow.py` | 默认验收器语义；工作流“只有 lint”被拒、补跑测试后完成 |

默认计划刻意保持保守：`protected_patterns=()` 与 `change_to_tests=()`。原因是通用入口无法预知哪些
测试文件属于既有产物，若默认按 `test_*.py` 全禁写入会误伤合法的测试创建；需要检测测试篡改或
无关测试绿灯的宿主（如对照评测）应显式构造带模式与映射的计划。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   783 passed, 2 skipped, 4 deselected
ruff check src tests                            All checks passed
ruff format --check src tests                   141 files already formatted
python -m compileall -q src tests               passed
```

关键回归：`test_coding_requires_a_test_run_not_only_lint` 证明写入后仅 lint 绿灯会触发
`verification_rejected`，补跑测试后才进入审查并完成；既有工作流与 CLI/REPL 用例保持通过。

## 尚未完成

- 让宿主按工作区预存测试显式声明 `protected_patterns`/`change_to_tests`，并在对照评测中启用，
  以真正覆盖“测试篡改/删除”和“无关测试绿灯”；
- 阶段 3 遗留的完整 90-trial 与 holdout（与阶段 4 无耦合）。
