# Iteration 034：工作区测试保护与变更—测试关联

## 状态与边界

状态：实现与离线验证已完成；真实 90-trial 仍留到代码冻结后运行。

本轮为阶段 4 Task 4.3：让宿主按工作区既有测试构造计划，在通用入口之外真正启用“测试篡改/删除”
与“无关测试绿灯”检测，并在三种架构对照评测中启用。它仍然只读已有 ToolResult、不执行命令。

## 动机

Iteration 033 的默认计划刻意不含受保护模式与关联映射，因此篡改既有测试、或跑无关测试骗绿灯
仍不会被拒。要覆盖这两类攻击，宿主需要知道运行前存在哪些测试、以及哪个变更对应哪个测试。

## 实现

| 文件 | 修改 |
|---|---|
| `src/core/completion.py` | 新增 `discover_test_files(root)`（不跟随符号链接、跳过 `__pycache__`）与 `plan_for_workspace(root, change_to_tests=...)` |
| `src/evaluation/compare_modes.py` | `_agent` 改用按工作区构造的 `PlannedCompletionVerifier`：既有测试受保护，`solution.py` 改动必须伴随真实运行 `test_target.py` |
| `tests/test_verification_plan.py` | 既有测试保护、新建测试不被阻止、篡改与无关绿灯被拒、关联测试通过 |

关键设计：

- `discover_test_files` 只列出**运行前已存在**的 `test_*.py`/`*_test.py`，把它们设为受保护；尚不存在
  的测试文件名不纳入，因此不阻止合法的测试创建。
- `plan_for_workspace` 把发现的相对路径作为保护模式，并接受宿主声明的 `change_to_tests` 映射。
- 对照评测计划：`protected_patterns=("test_target.py",)`，
  `change_to_tests=(("solution.py", ("test_target.py",)),)`；改写 `test_target.py` 直接拒绝，
  只跑无关测试则报“缺少关联测试绿灯”。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   785 passed, 2 skipped, 4 deselected
pytest tests/test_verification_plan.py          11 passed
ruff check src tests                            All checks passed
ruff format --check src tests                   141 files already formatted
python -m compileall -q src tests               passed
```

## 尚未完成

- 真实 90-trial 回归与 holdout（需代码冻结后运行）；本轮只做了离线门禁与单元验证；
- 阶段 6 发布演示（依赖冻结后的完整评测）。
