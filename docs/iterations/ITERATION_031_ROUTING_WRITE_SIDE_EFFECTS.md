# Iteration 031：修复中文写入副作用盲点并量化误伤

## 状态与边界

状态：实现、离线验证已完成；未调用真实模型。

本轮修复 Iteration 030 记录的路由盲点：中文只读动词开头叠加“写入/保存”等副作用时，rules 分类
与宿主守卫都判为简单。修复把中文写入/保存副作用动词集中为 `WRITE_SIDE_EFFECTS`，供
`routing.intent.classify` 与 `routing.classifier.require_strong` 共用，避免两处词表漂移；并用
校准数据集量化由此带来的保守升级（误伤）。

## 动机

Iteration 030 的校准显示 `读取数据并写入新文件`、`列出文件并写入缓存` 被 rules 判为 simple，
且 `require_strong` 同样漏判；`读取配置并保存` 在 rules 路径也被降级。这类任务有产物副作用，
降级到轻量模型的收益不足以覆盖风险。修复的同时必须量化“只读却含写入字样”（如
`查看日志中的写入记录`）被升级的成本，避免用扩大词表换取误伤而不自知。

## 实现

| 文件 | 修改 |
|---|---|
| `src/routing/intent.py` | 新增 `WRITE_SIDE_EFFECTS`（写入/写到/写出/写下/写回/另存/保存/追加/替换），并入 `_COMPLEX`；补 `新建` |
| `src/routing/classifier.py` | `_RISKY_TASK` 复用 `WRITE_SIDE_EFFECTS`，与 rules 词表对齐 |
| `tests/test_routing.py` | `test_intent_rules` 增加 3 条写入/保存应判复杂 |
| `tests/test_model_intent_routing.py` | 宿主守卫参数化增加 2 条中文写入任务 |
| `src/evaluation/routing_calibration_cases.json` | 数据集升 v2：盲点样本转正、新增误伤探测样本 |
| `tests/test_routing_calibration.py` | 更新 v2 指标，新增 known/unexpected 分离计数用例 |
| `docs/ARCHITECTURE.md`、`docs/EVALUATION.md` | 说明共用词表与 v2 结果 |

## 度量结果（校准数据集 v2）

```text
total_cases                28   （simple 9 / complex 19）
rules_accuracy             0.928571
rules_false_downgrades     0
rules_unexpected_downgrades 0
rules_false_escalations    2    （误伤成本）
guarded_false_downgrades   0
```

误伤（保守升级）2 例：`读取 README`（既有空白边界）与新增的 `查看日志中的写入记录`（只读查询
含“写入”）。错误降级降为 0，未预期降级回归门保持 0。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   773 passed, 2 skipped, 4 deselected
pytest tests/test_routing_calibration.py         8 passed
ruff check src tests                             All checks passed
ruff format --check src tests                    140 files already formatted
python -m compileall -q src tests                passed
```

RED 证据：先加 5 条断言（rules 3 条 + 宿主守卫 2 条），实现前 `5 failed, 22 passed`；实现后
全部 GREEN。

## 尚未完成

- 若误伤不可接受，可把 `WRITE_SIDE_EFFECTS` 从“任意出现即复杂”改为“只读动词 + 写入目标”的
  上下文匹配，需要额外的标注样本评估；
- 真实模型分类（`intent_strategy="model"`）的准确率仍需有真实样本后另测；
- 阶段 3 遗留的完整同协议 90-trial 与 holdout（与本轮修复无耦合）。
