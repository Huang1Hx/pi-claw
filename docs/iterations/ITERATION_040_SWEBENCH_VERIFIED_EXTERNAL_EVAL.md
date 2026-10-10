# Iteration 040：SWE-bench Verified 外部评测脚手架（进行中）

## 状态与边界

状态：**进行中**。已完成数据筛选、隔离脚手架与端到端 pilot；尚未产出"本项目 Agent 在公开
数据集上的解决率"这一最终数字。

目标：在与冻结评测**完全隔离**的前提下，用公开数据集 SWE-bench Verified 给项目一个外生对照，
避免"用自己出的题自证"。

隔离保证：

- 代码位于独立的 `external_eval/`，**不导入也不修改** `evaluation.compare_modes` 等冻结评测入口；
- 产物只写 `runs/swebench-verified/`，并有 `assert_isolated_output` 拒绝 `stage3-*`/`agent-modes-*`
  前缀；
- 不进入主 `pytest`（`testpaths=["tests"]`）与 CI（`ruff check src tests`）范围，互不干扰；
- 外部实例 id 与冻结任务 id 断言无重叠。

## 任务筛选（数据驱动）

从 Verified 全量 500 条中，按"纯 Python、依赖轻、测试快、gold 补丁单文件且新增行少、
FAIL_TO_PASS ≤ 8"筛出 12 条，覆盖 `requests`/`pytest`/`pylint`/`sphinx`：

| 仓库 | 实例 |
|---|---|
| psf/requests | 1142、1766、1921、5414 |
| pytest-dev/pytest | 5809、6202、7521、7982 |
| pylint-dev/pylint | 4970、7080 |
| sphinx-doc/sphinx | 8595、8269 |

清单固定在 `external_eval/selection.json`，`select_instances` 会因缺项/越界仓库直接报错，保证可复现。

## 脚手架

| 文件 | 作用 |
|---|---|
| `external_eval/swebench_verified.py` | 分页拉取并缓存元数据、清单加载、测试清单解析、隔离断言、冻结任务 id 只读校验 |
| `external_eval/harness.py` | 克隆 base_commit、建 venv（可用 `uv` 固定 Python 版本）、`git apply` 补丁、按官方规则判定 resolved |
| `external_eval/__main__.py` | `fetch` / `list` / `prepare` / `gold` / `patch` 子命令 |
| `external_eval/tests/test_external_eval.py` | 7 项离线测试（筛选、隔离、判定逻辑、uv argv），不联网 |
| `external_eval/docker_env.py` | 方案 B：按仓库固定 Python 的受限容器（`--memory 2g --cpus 2 --pids-limit 512`），准备期联网装依赖、判定期 `--network none` |
| `external_eval/tests/test_docker_env.py` | 4 项离线测试（镜像映射、资源上限、network none、安装 argv），不真的起容器 |

判定规则与官方一致：应用预测补丁与 `test_patch` 后，**全部 FAIL_TO_PASS 通过且全部 PASS_TO_PASS
仍通过**才算 resolved。

## 侦察结论（决定后续路线）

1. **冻结策略不能直接跑公开数据集**：项目策略白名单只允许 `uv run pytest/ruff`、`python -m
   unittest/compileall`、只读 git 与文件编辑，Docker 沙箱为 30s/512MiB/无网络；SWE-bench 需要按仓库
   装配依赖并跑较长测试。因此需要一条**独立的、放宽的外部执行 profile**，而不是改冻结策略。
2. **本地 venv 判定有环境噪声**：pilot 用 `psf__requests-5414` 的 gold 补丁自检，FAIL_TO_PASS
   1/1 通过，但 PASS_TO_PASS 121/130——9 条失败集中在 `doesnotexist.google.com` 连接、
   `connect_timeout`、`socks` 代理环境等**与补丁无关的网络/环境**用例。换 Python 3.9 结果相同。
   说明可信数字需要**固定环境**（官方 per-instance Docker 镜像，或等价的可控镜像）。
3. **架构差异**：本机为 aarch64，而 SWE-bench 官方镜像多为 x86_64，需模拟运行，速度与镜像体积
   都是成本项。

## 验证

```text
external_eval 离线单元测试                  7 passed
gold pilot（requests-5414，Python 3.12）    ftp=1/1，ptp=121/130（环境噪声）
gold pilot（requests-5414，Python 3.9）     ftp=1/1，ptp=121/130（同上，非版本问题）
冻结评测未改动                              src/evaluation 与 runs/stage3-* 不受影响
```

## 后续（待确认）

- 执行环境已定为**方案 B**（`docker_env.py`，按仓库固定 Python 的受限容器）；
- 实现外部 Agent 适配器：以放宽的、**独立**的 policy/审批驱动本项目 Agent 产出补丁；
- 先在 3–5 条实例上跑通"Agent 产补丁 → 官方规则判定"，报告 resolve 率、Token、步数与耗时；
- 结果写入本文件并作为外生对照，不改动任何冻结评测的任务与口径。

### 待办阻塞

容器版 gold 自检已实跑，但**固定 Python 版本被网络限制卡住**：

- Docker 守护进程无法访问 `registry-1.docker.io`，`python:3.9-slim` 拉不下来；镜像源
  `docker.m.daocloud.io` 亦超时。
- 容器内 `pip` 可访问 PyPI（200），但用 `uv` 在容器内拉取独立 CPython 3.9 的下载挂起。
- 退而用本地已有的 `python:3.12.15` 镜像 + 判定期 `--network none` 跑通：
  `psf__requests-5414` gold `ftp=1/1`、`ptp=124/130`（较宿主 venv 的 121/130 少 3 项网络噪声）。
  剩余 6 项失败为 **Python 3.12 vs 3.9 语义**（`_basic_auth_str` 原生 str、socket 超时行为），
  仍属环境而非补丁问题。

因此需要一个可用的固定版本来源（预拉取镜像 / 可用镜像源 / 预置 3.9 运行时）才能得到干净基线。

**当前可行的替代口径**：以 gold 补丁在同环境的 PASS_TO_PASS 结果作为该实例基线，
只统计"相对基线的回归"，用 `FAIL_TO_PASS` 通过率作为修 bug 的主信号——这样 3.12 的固定环境噪声
被基线抵消，仍能给出可信的相对结论。
