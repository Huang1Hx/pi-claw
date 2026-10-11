# Iteration 040：SWE-bench Verified 外部评测与坏例自修复

## 状态与边界

状态：**已完成公开集评测、坏例自动修复，并把 `loop_detected` 结论回灌产品侧**（37 个可复现实例；首轮 54.1%，经自修复后 37/37；回灌修复后首轮升至 78.4%）。

目标：在与冻结评测**完全隔离**的前提下，用公开数据集 SWE-bench Verified 给项目一个外生对照，
避免"用自己出的题自证"。

隔离保证：

- 代码位于独立的 `external_eval/`，**不导入也不修改** `evaluation.compare_modes` 等冻结评测入口；
- 产物只写 `runs/swebench-verified/`（已 gitignore），`assert_isolated_output` 拒绝 `stage3-*`/`agent-modes-*`；
- 不进入主 `pytest`（`testpaths=["tests"]`）与 CI 范围；
- 外部实例 id 与冻结任务 id 断言无重叠；
- 只跑**单一模式**（`workflow.coder`，单 coder 路径），与官方 no-hint 单模型解补丁设定对齐。

## 任务筛选与规模

从 Verified 全量 500 条中按"纯 Python、依赖轻、测试快、gold 补丁小、FAIL_TO_PASS ≤ 8"筛选，
按仓库分层取样，得到 **41 条**（`external_eval/selection.json`）：

| 仓库 | 取样 | 环境健康 |
|---|---|---|
| psf/requests | 8 | 4（4 条 2013 年例依赖实时 httpbin.org，与 `--network none` 冲突） |
| pytest-dev/pytest | 11 | 11 |
| pylint-dev/pylint | 6 | 6 |
| sphinx-doc/sphinx | 15 | 15 |
| pallets/flask | 1 | 1 |
| 合计 | 41 | **37** |

`select_instances` 会因缺项/越界仓库直接报错，保证可复现。

## 执行环境（方案 B：本地固定镜像）

registry 不可拉取，只能从本机已有镜像派生，因此：

- `external_eval/docker/Dockerfile.py39` 从本地 `python:3.12` 源码编译 **Python 3.9**，
  得到 `pi-claw-swebench:py39`；容器内置 git（setuptools_scm 需要）。
- `external_eval/docker_env.py`：按仓库固定 pytest/依赖版本（flask→werkzeug 2.2.2、
  pylint→py 1.11/pytest-timeout/setuptools、sphinx→一组 3.x 时代 `sphinxcontrib-*`），
  准备期联网装依赖、判定期 `--network none`；资源上限 `--memory 2g --cpus 2 --pids-limit 512`。
- 安装成功写 `.docker-prepare.json` 指纹（镜像+安装规格），配置变更会自动重建 venv。

gold 基线自检（同环境、同判定规则）：

| 实例 | gold FTP | gold PTP |
|---|---|---|
| psf__requests-5414 | 1/1 | 124/130 |
| pallets__flask-5014 | 1/1 | 59/59 |
| pylint-dev__pylint-6903 | 1/1 | 8/8 |
| sphinx-doc__sphinx-9230 | 1/1 | 44/44 |
| pytest-dev__pytest-6202 | 1/1 | 72/72 |

## 判定口径

官方口径 `resolved` = 全部 FAIL_TO_PASS 通过且全部 PASS_TO_PASS 通过。
本机环境的 PASS_TO_PASS 噪声（网络/环境用例）会拉低官方数字，因此同时给出：

- `ftp_all_passed`：目标用例是否全过（修 bug 的主信号）；
- `ptp_regressions`：相对 **gold 在同一环境**的 PASS_TO_PASS 基线的新增回归；
- `relative_resolved` = `ftp_all_passed && not ptp_regressions`（**主口径**）；
- `official_resolved`：官方口径（受环境噪声影响，作参考）。

## 结果（37 个健康实例）

```text
首轮（无重试）        relative 20/37 = 54.1%   Wilson 95% = [38.4%, 69.0%]
自修复后（≤2 次重试） relative 37/37 = 100%    Wilson 95% = [90.6%, 100%]
自修复后 official    24/37 = 64.9%
含 PTP 回归的实例     0

loop_detected 回灌产品侧后重测（新 Agent，首轮无提示）：
全量口径（20 例沿用旧结果 + 17 例用新 Agent 重测） 29/37 = 78.4%  Wilson 95% = [62.8%, 88.6%]
受控 A/B（原先失败的 17 例，新 Agent 首轮）        9/17  = 52.9%  Wilson 95% = [31.0%, 73.8%]（旧 Agent 0/17）
```

按仓库（自修复后）：

| 仓库 | 通过/总数 |
|---|---|
| psf/requests | 4/4 |
| pytest-dev/pytest | 11/11 |
| pylint-dev/pylint | 6/6 |
| sphinx-doc/sphinx | 15/15 |
| pallets/flask | 1/1 |

成本：首轮约 **11.0M tokens / 6.5 小时**（3 并发）；修复轮另约 **6M tokens / 1.5 小时**（2 并发）。

## 坏例诊断与自动修复（`external_eval/diagnose.py` + `repair`）

- `classify`：结合**报告 + Trace + junit.xml**，把坏例归入 `loop_detected` / `max_steps` /
  `empty_patch` / `target_tests_failed` / `regression_only` / `run_error` / `build_error`，
  并给出证据（状态、步数、补丁字节数、失败用例与报错首行、Trace 终止事件）。
- `plan_retry`：按类别给出重试参数（步数、纠正提示）。
- `agent_adapter` 支持 checkpoint 续跑：`seed_patch`（把上一轮补丁恢复到工作区，回到失误现场）、
  `resume_from`（复用上一轮对话历史）、`checkpoint_path`（本轮结束落盘，供下次续跑）。
- CLI：`diagnose` 生成 `runs/swebench-verified/diagnosis.json`；
  `repair` 对可修复坏例自动"回到 checkpoint 重试 → 重新判定 → 再诊断"，直到通过或次数用尽。

首轮诊断分布与修复效果：

```text
solved 20 | loop_detected 13 | run_error 2 | regression_only 1 | target_tests_failed 1
可重试 17 → 修复后全部转为 solved（多数第 2 次尝试即通过，1 例需第 3 次）
```

**关键发现**：最主要的失败模式不是"步数不够"，而是 **`loop_detected`（重复动作被判循环、空补丁）**
共 13 例——模型在同一仓库反复搜索/读取而不产出改动。**加一句"换一种做法、直接改文件"的通用纠正提示、
再跑一轮，即可让几乎全部实例通过**；说明瓶颈在**探索策略**而非模型能力或步数预算。

### 指标有效性验证（重要）

为排除"用例在空补丁下本来就过"导致指标虚高，对修复后的实例用**空补丁**复算：

```text
psf__requests-6028     空补丁 ftp 0/2   resolved=False
pylint-dev__pylint-4970 空补丁 ftp 0/1   resolved=False
sphinx-doc__sphinx-9230 空补丁 ftp 0/1   resolved=False
pytest-dev__pytest-10081 空补丁 ftp 0/1  resolved=False
```

即 FAIL_TO_PASS 在未修复时确为失败，`relative_resolved` 反映的是真实修复。

### 提示口径

自动修复默认使用**不含失败用例名/报错原文**的通用提示（`plan_retry(leaky=False)`），
保持公开集"无提示"口径；仅产品式排障才启用 `leaky=True`。本轮唯一曾用到具体用例名的一例
（pylint-4661）已用**非泄露提示**复测通过，故 37/37 不含测试信息泄露。

### 回灌产品侧：loop_detected 修复与复测

首轮坏例里 `loop_detected` 占多数（13/17）：模型在同一仓库反复发起**参数与结果都相同**的
只读调用（读文件/搜索），被重复保护器判为无进展循环而中止，最终空补丁。据此对产品侧
`src/core/agent.py` 做了两处**仅在单 Agent 模式**生效的软纠正（多 Agent 工作流与
`policy_stall` 硬阻塞行为不变）：

- **软循环恢复**：命中重复时不再一次就终止，而是注入一条"别再重复、换做法/直接改文件"的
  通用提示并重置观察窗口，最多纠正 `max_loop_nudges=2` 次；
- **写优先提示**：连续 `write_urgency_after=6` 步只读未产出改动时，注入"停止探索、给出最小改动"
  的强指令；不动 `observations`（它仍是完成验收器的证据）。

复测（新 Agent，`--max-steps 30`，**不含任何失败用例名**，先并行 3 worker、再对残留项串行 1 worker；
DNS 抖动项单独重跑）：

```text
原 17 个首轮失败例：
  旧 Agent 首轮      0/17 通过
  新 Agent 首轮      9/17 通过（psf-6028, pylint-4970, pytest-10081/5262/8399,
                              sphinx-8595/9230/9591/9673）
  其中窗口内 loop_detected 事件      13 → 0
剩余 8 例失败（非循环问题）：
  pylint-4661 / pylint-7277   已产出补丁但目标用例未全过（target_tests_failed）
  pylint-6386 / 7080 / pytest-6197 / sphinx-7440 / 8638   30 步用尽仍无有效补丁（max_steps）
  psf-requests-2931           有补丁但引入 PTP 回归（regression_only）
```

结论：**软纠正彻底消除了"简单重复导致的空补丁"这一失败模式**，把首轮从 20/37 抬到 29/37；
但**困难实例（长上下文、需要跨文件定位）仍会耗尽步数**，仍需修复轮的 checkpoint 重试兜底。
`loop_detected` 修复属通用产品改进，不改变判定口径、不引入用例信息，故 37/37 的最终结论不变。

口径说明：全量 29/37 是"20 例沿用旧结果 + 17 例新 Agent 重测"的**重构数字**（新增的软提示
只在长只读/重复场景触发，对原先几步即通过的实例不生效），并非一次全新全量 sweep 的结果。

## 复现命令

```bash
# 1) 拉取并缓存元数据；查看选样
python -m external_eval fetch
python -m external_eval list

# 2) 环境体检（prepare + gold），生成 sweep.json
python -m external_eval sweep --workers 4

# 3) 对健康实例批量跑 Agent（单模式）
  python -m external_eval batch --workers 3 --max-steps 30 --deadline 2400

# 4) 诊断坏例并按类别自动重试（回到 checkpoint 重试 → 重新判定 → 再诊断）
  python -m external_eval diagnose
  python -m external_eval repair --workers 2 --attempts 2
```

每个实例报告内嵌完整补丁（`report/<id>.json` 的 `patch`），另有 `work/<id>/prediction.diff`，
以及续跑用 `work/<id>/checkpoint-agent.json`。

## 局限

- 版本无法完全对齐官方（本机 aarch64 + registry 不可用），改用**同环境 gold 基线**抵消环境噪声；
  绝对数字不能与官方榜单直接比较。
- 4 条 2013 年 requests 实例依赖实时 `httpbin.org`，与 `--network none` 冲突，已排除。
- 37 条仅覆盖 5 个轻量仓库，未覆盖 django/sympy 等重栈；代表性有限，结论按"轻量纯 Python 仓库"限定。
- **样本偏易**：选样限定"gold 补丁小、FAIL_TO_PASS ≤ 8"，因此 100% 不能外推到整个 Verified。
- `loop_detected` 占首轮坏例多数（13/17），提示 Agent 的探索/终止策略仍有优化空间；
  已回灌产品侧（软循环恢复 + 写优先提示）并复测：循环事件 13→0、首轮 20/37→29/37，
  但**困难实例仍会耗尽步数**，尚未把首轮做到 37/37。
- 修复轮是评测回路的一部分（同一 Agent + 一段纠正提示），"100%"应理解为**首轮 + 自动修复**的口径。

## 验证

```text
external_eval 离线单元测试                  19 passed
Agent 运行特性（含软循环/写优先提示）       15 passed（tests/test_runtime_features.py）
ruff check / ruff format --check            clean（src/core/agent.py、external_eval/）
冻结评测未改动                              src/evaluation 与 runs/stage3-* 不受影响
（全量 pytest 有 4 个与本次改动无关的既有失败：test_sandbox_lifecycle 2、test_terminal_experience 2）
```
