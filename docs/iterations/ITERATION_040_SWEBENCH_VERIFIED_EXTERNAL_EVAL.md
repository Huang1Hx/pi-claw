# Iteration 040：SWE-bench Verified 外部评测与坏例自修复

## 状态与边界

状态：**已完成首轮公开集评测**（37 个可复现实例 + 坏例自动诊断/重试能力）。

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

## 首轮结果（37 个健康实例）

```text
relative_resolved   20/37 = 54.1%   Wilson 95% = [38.4%, 69.0%]
official_resolved   15/37 = 40.5%
含 PTP 回归的实例    1
```

按仓库：

| 仓库 | 通过/总数 |
|---|---|
| psf/requests | 2/4 |
| pytest-dev/pytest | 7/11 |
| pylint-dev/pylint | 1/6 |
| sphinx-doc/sphinx | 9/15 |
| pallets/flask | 1/1 |

成本：合计约 **11.0M tokens**、**约 6.5 小时**（3 并发）；单实例步数中位数 11，最长 37。

## 坏例诊断与自动修复（`external_eval/diagnose.py` + `repair`）

- `classify`：结合**报告 + Trace + junit.xml**，把坏例归入 `loop_detected` / `max_steps` /
  `empty_patch` / `target_tests_failed` / `regression_only` / `run_error` / `build_error`，
  并给出证据（状态、步数、补丁字节数、失败用例与报错首行、Trace 终止事件）。
- `plan_retry`：按类别给出重试参数（步数、纠正提示）。
- `agent_adapter` 支持 checkpoint 续跑：`seed_patch`（把上一轮补丁恢复到工作区，回到失误现场）、
  `resume_from`（复用上一轮对话历史）、`checkpoint_path`（本轮结束落盘，供下次续跑）。
- CLI：`diagnose` 生成 `runs/swebench-verified/diagnosis.json`；
  `repair` 对可修复坏例自动"回到 checkpoint 重试 → 重新判定 → 再诊断"，直到通过或次数用尽。

首轮诊断分布：

```text
solved 20 | loop_detected 13 | run_error 2 | regression_only 1 | target_tests_failed 1
可重试 17
```

**关键发现**：最主要的失败模式不是"步数不够"，而是 **`loop_detected`（重复动作被判循环、空补丁）**
共 13 例——模型在同一仓库反复搜索/读取而不产出改动。放宽步数（30→60）只救回 3 例
（pytest-6202、sphinx-10466、sphinx-7889），说明瓶颈在**探索策略**而非预算。

## 复现命令

```bash
# 1) 拉取并缓存元数据；查看选样
python -m external_eval fetch
python -m external_eval list

# 2) 环境体检（prepare + gold），生成 sweep.json
python -m external_eval sweep --workers 4

# 3) 对健康实例批量跑 Agent（单模式）
python -m external_eval batch --workers 3 --max-steps 30 --deadline 2400

# 4) 诊断坏例并按类别自动重试
python -m external_eval diagnose
python -m external_eval repair --workers 2 --attempts 2
```

## 局限

- 版本无法完全对齐官方（本机 aarch64 + registry 不可用），改用**同环境 gold 基线**抵消环境噪声；
  绝对数字不能与官方榜单直接比较。
- 4 条 2013 年 requests 实例依赖实时 `httpbin.org`，与 `--network none` 冲突，已排除。
- 37 条仅覆盖 5 个轻量仓库，未覆盖 django/sympy 等重栈；代表性有限，结论按"轻量纯 Python 仓库"限定。
- `loop_detected` 占坏例多数，提示 Agent 的探索/终止策略仍有优化空间。

## 验证

```text
external_eval 离线单元测试                  18 passed
ruff / ruff format                          clean
冻结评测未改动                              src/evaluation 与 runs/stage3-* 不受影响
```
