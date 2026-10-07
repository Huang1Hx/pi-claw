# Iteration 026：Docker 攻击评测与量化安全门

## 状态与边界

状态：开发中（Task 3.8 的离线安全和远程 CI 完成；模型诊断复测、90-trial 和 holdout 待完成）。

本轮不调用 DeepSeek 或 Kimi，不修改 grader、权限白名单、420 秒模型 deadline、24 steps、最多两轮
修复或 Iteration 021 的 `78/90` 冻结基线。原始本地报告保存在 Git 忽略的 `runs/`，不提交 argv、
stdout、宿主路径、canary、API Key、Base URL、Trace 或 Session。

## 目标

把“Docker 参数看起来安全”升级为可重复、可量化的运行证据：

1. 11 类确定性攻击恰好各运行一次，身份重复或脚本故障均 fail closed；
2. 记录泄漏、网络、宿主副作用、逃逸、清理和残留资源硬门槛；
3. 连续运行 30 个轻量容器，给出完成/清理率、P50/P95 启动与总耗时、峰值缓冲；
4. 同一只读任务在 Host/Docker 返回码、stdout、stderr 和源工作区摘要一致；
5. Docker 缺失时本地 integration 可跳过，但发布 CI 不允许把跳过当成功。

## 数据流

```text
固定攻击目录
  → DockerCommandRunner
  → 去敏快照 /input:ro
  → 私有 /workspace tmpfs
  → 非 root + network=none + 资源限制
  → CommandExecution 数值元数据
  → 用例特定判据
  → 脱敏聚合报告
  → 安全硬门 + 30-trial 门 + Host/Docker parity
```

`first_output_seconds` 从 Docker Runner 入口开始，包含镜像复核和快照准备；`duration_seconds` 还包含
容器和快照清理。无限循环用例从实际 timeout 时点开始计量清理，而不是用总耗时减 deadline 估算。

## 修改范围

- `src/evaluation/sandbox_cases.py`：攻击目录、用例特定判定、连续试验、parity、create-only 报告。
- `src/sandbox/host.py`：安全的首输出、总耗时、缓冲和超时清理数值元数据。
- `src/sandbox/docker.py`：只透传白名单指标，并把快照/镜像复核/容器清理纳入端到端计时。
- `tests/test_docker_sandbox_integration.py`：单元反误报契约和真实 Docker 集成用例。
- `.github/workflows/sandbox.yml`：构建本地镜像并强制执行 Docker integration；Docker 不可用即失败。
- `pyproject.toml`：注册 `docker_integration` marker。
- 安全、运行时、架构、README 和阶段计划：同步实现边界与当前证据。

## TDD 与审查记录

先写失败测试，再实现：

- 缺少 `evaluation.sandbox_cases` 导致 collection RED，随后实现目录和报告；
- 重复攻击身份仍能通过、空输出/普通失败码被误判阻断，两个测试先 RED 后修复；
- 少于 30 个连续 trial 仍可返回成功，新增最终 acceptance 门后 GREEN；
- 初版网络接口名判据在 Docker Desktop 的隧道占位接口上 RED，定位 `/proc/net/route` 后改为
  “无外部 IPv4 路由且实际连接失败”，真实攻击集恢复 GREEN；
- 超时清理由估算改为 Host + Docker 两层实际计时，并加入专项断言。

独立复审代理因账户用量上限未能产出结论；本批执行了人工 diff 审查、反误报测试、完整回归、
Ruff、格式、编译、构建、敏感值精确扫描和残留容器检查。该限制不伪装成独立 Ready。

## 2026-10-07 本机量化结果

命令：

```bash
uv run --frozen python -m evaluation.sandbox_cases \
  --output runs/stage3-sandbox-eval-20261007-r2.json --trials 30
```

| 指标 | 结果 |
|---|---:|
| 攻击阻断 | 11/11（100%） |
| 宿主/凭据泄漏 | 0 / 0 |
| 网络成功 / 原工作区副作用 / 逃逸 | 0 / 0 / 0 |
| 清理失败 / 残留资源 | 0 / 0 |
| 超过 5 秒的 timeout 清理 | 0 |
| 连续 trial 完成率 / 清理率 | 30/30 / 30/30 |
| 启动耗时 P50 / P95 | 0.176391s / 0.190852s |
| 总耗时 P50 / P95 | 0.232114s / 0.252647s |
| 峰值保留输出缓冲 | 6 bytes |
| Host/Docker 确定性 parity | 通过 |
| 最终离线 acceptance | 通过 |

性能数字只描述本机本次运行，不是跨机器 SLO；安全指标才是硬门槛。

## 提交前验证

```text
pytest                                      731 passed
ruff check src tests                        passed
ruff format --check src tests               136 files already formatted
python -m compileall -q src tests            passed
uv build --no-config                        wheel + sdist passed
真实 Key 与私有学校 URL 精确值扫描          0 match
Docker residual pi-claw containers          0
```

Moonshot 的公开 Base URL 本来就存在于 `.env.example` 和 API Key 指南，不作为私有地址泄漏；实际
Moonshot Key、学校 Key 和学校 Base URL 均未命中 tracked/new 文件或构建产物。

## 尚未完成

- 只复测 Iteration 021 冻结的 12 个未恢复身份，不替换 `78/90`；
- 同协议完整 90-trial 回归和至少 27-trial 新 holdout；
- 最终覆盖率与 Stage 3 总结。真实模型步骤只能在上述离线门和远程 CI 都通过后开始。

## 远程 CI 证据

`optimize` 的提交 `f66dada` 触发 GitHub Actions `sandbox-security` run `37567045236`：镜像构建、
强制 Docker integration 和零残留容器检查均通过，job 用时 58 秒。首次运行提示旧官方 Action 的
Node 20 声明已弃用，随后把 checkout/setup-python 升到各自官方最新发布对应的不可变 commit；
下一次远程运行继续验证该维护修复。
