# Iteration 030：离线路由校准与复杂任务错误降级度量

## 状态与边界

状态：实现、离线验证已完成；未调用真实模型。

本轮为阶段 5 Task 5.3：建立带人工标签的 simple/complex 数据集，离线度量确定性规则分类器与宿主
安全网的准确率、复杂任务错误降级率与保守升级率。它只做度量与回归门，不修改现有分类器判定，
也不引入价格、节省率或真实模型调用。

## 动机

`intent_strategy="rules"` 由 `routing.intent.classify` 直接决定轻量/主力，但此前没有带标签数据
证明它在真实任务分布上的准确率，也没有量化“复杂任务被降级到轻量模型”的安全风险。同时，
`require_strong` 只在 `model` 策略路径上作为宿主兜底，其在错误降级上的覆盖面也从未度量。本轮把
这些都变成可复现、可审计的离线指标。

## 实现

| 文件 | 修改 |
|---|---|
| `src/evaluation/routing_calibration_cases.json` | 26 条带标签样本（8 simple / 18 complex），含刻意探测规则边界的对抗样本 |
| `src/evaluation/routing_calibration.py` | `load_calibration_suite`（fail closed）与 `evaluate_routing_calibration`（离线度量并写报告） |
| `src/interfaces/cli.py` | 新增 `eval-routing-calibration` 子命令，未预期降级为 0 时返回 0 |
| `tests/test_routing_calibration.py` | 指标、可审计行、加载器 fail-closed 与 CLI 冒烟 |
| `docs/ARCHITECTURE.md`、`docs/EVALUATION.md` | 指标、边界与命令说明 |

关键设计：

- 每条样本运行两条决策：`rules_decision`（rules 策略真实行为）与
  `guarded_decision = complex if require_strong else rules_decision`（叠加宿主安全网的保守决策）。
- `complex→simple` 记为错误降级（安全风险），`simple→complex` 记为保守升级（成本）。
- `known_downgrade` 显式标记已接受的规则盲点，计入 `rules_known_downgrades`；未标记的错误降级
  计入 `rules_unexpected_downgrades`，作为必须为 0 的回归门。
- 加载器 fail closed：`schema_version=1`、非空 `name`、正整数 `version`、非空 `cases`、
  `id` 唯一且非空、`label∈{simple,complex}`，`known_downgrade` 仅允许用于 complex。

## 度量结果（固定数据集 v1）

```text
total_cases            26   （simple 8 / complex 18）
rules_accuracy         0.884615
rules_false_downgrades 2    （均为 known_downgrade）
rules_unexpected_downgrades 0
rules_false_escalations 1
guarded_accuracy       0.884615
guarded_false_downgrades 2
```

已记录盲点：中文只读动词开头叠加“写入/写到”副作用（如 `读取数据并写入新文件`、
`列出文件并写入缓存`）不在复杂词表，规则与宿主安全网都未拦截，显式标记为 `known_downgrade`。
本次不修改判定；修复列为后续候选。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   767 passed, 2 skipped, 4 deselected
pytest tests/test_routing_calibration.py         7 passed
ruff check src tests                             All checks passed
ruff format --check src tests                    140 files already formatted
python -m compileall -q src tests                passed
```

RED 证据：先移走 `routing_calibration.py`，`tests/test_routing_calibration.py` 收集期
`ModuleNotFoundError`；实现后 7 项 GREEN。

## 尚未完成

- 候选修复：把中文“写入/写到”等副作用表达补入复杂词表或宿主安全网（需单独评估误伤）；
- `model` 策略（真实模型分类）的准确率需在有真实样本后另行度量，不在本轮离线范围内；
- 阶段 3 遗留的完整同协议 90-trial 与 holdout（与本轮校准无耦合）。
