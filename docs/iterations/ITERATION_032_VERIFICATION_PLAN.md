# Iteration 032：宿主侧 VerificationPlan 与语义完成验收

## 状态与边界

状态：核心实现与离线验证已完成；未接入 CLI/REPL/工作流（Task 4.2 待做），未调用真实模型。

本轮为阶段 4 Task 4.1：新增宿主声明、不可变的 `VerificationPlan` 与
`PlannedCompletionVerifier`，把完成验收从“写入后有任一受认可检查通过”提升为有计划的语义验收。
它只检查已经发生并形成 ToolResult 的证据，不自行执行命令、不扩大 Agent 权限，也不改变既有
`ToolEvidenceCompletionVerifier` 的默认行为。

## 动机

旧验收器只要求写入后有任意一个受认可类别（test/lint/compile/lock）通过，无法阻止：改写或删除
测试文件、只跑可选检查而跳过测试、测试之后又写入、以及跑一个与改动无关的测试骗绿灯。阶段 4
要求模型不能自行降低验收要求，因此由宿主声明计划，验收器据此判定。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/completion.py` | 新增 `VerificationPlan`、`PlannedCompletionVerifier`、`VERIFICATION_KINDS` 与测试文件识别；保留旧验收器 |
| `tests/test_verification_plan.py` | 计划校验、篡改、必跑、复测与无关绿灯用例 |
| `docs/ARCHITECTURE.md` | 两层验收与 `VerificationPlan` 说明 |

关键设计：

- `VerificationPlan` 冻结不可变：`required_kinds`（必跑，非空）、`optional_kinds`、`protected_patterns`
  （fnmatch）、`change_to_tests`（变更文件→关联测试）。必跑/可选不相交、类别合法、模式非空、映射
  键唯一，任何非法组合 fail closed。
- `PlannedCompletionVerifier.verify` 检查链：无成功写入→无需验收；写入受保护文件→拒绝（测试篡改/
  删除）；取最后一次成功写入之后的受认可运行；每个必跑类别必须有成功运行；存在 `change_to_tests`
  映射时，每个有映射的变更文件必须有成功测试运行引用其关联测试，否则拒绝（无关测试绿灯）。
- 测试文件识别只看 argv 里像 `test_*.py`/`*_test.py` 的路径，仅用于关联判断，不执行命令。
- 旧 `ToolEvidenceCompletionVerifier` 保持不变，现有调用点语义不变。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   781 passed, 2 skipped, 4 deselected
pytest tests/test_verification_plan.py          8 passed
ruff check src tests                            All checks passed
ruff format --check src tests                   141 files already formatted
python -m compileall -q src tests               passed
```

RED 证据：先写 8 项测试，实现前收集期 `ImportError: cannot import name 'PlannedCompletionVerifier'`；
实现后 GREEN。

## 尚未完成

- Task 4.2：把 `VerificationPlan` 接入 CLI/REPL/工作流并给出默认计划，同时区分 eval 场景，避免
  误伤合法的测试创建；
- 阶段 3 遗留的完整 90-trial 与 holdout（与阶段 4 无耦合）。
