# Iteration 035：公开仓库许可证与通用 CI

## 状态与边界

状态：实现与离线验证已完成；工作流在 GitHub 上的首次真实运行待推送后确认。

本轮补齐阶段 1：为公开仓库添加 MIT `LICENSE` 与包元数据，并新增通用 CI（Lint、格式、编译、
测试、构建）。它不改变任何运行时行为，沙箱安全门工作流保持不变。

## 动机

仓库此前缺少许可证文件，也没有在推送/PR 上强制运行完整 pytest、Ruff、format 与构建的通用 CI，
只有 `.github/workflows/sandbox.yml` 覆盖 Docker 沙箱安全门。`plan11.md` 也明确指出 CI 未落地时
阶段 3 不能标记全部完成，因此这是发布前的必需项。

## 实现

| 文件 | 修改 |
|---|---|
| `LICENSE` | 新增 MIT 许可证（版权 `2026 Huang1Hx`） |
| `pyproject.toml` | 新增 PEP 639 `license = "MIT"` 与 `license-files = ["LICENSE"]` |
| `.github/workflows/ci.yml` | 新增通用 CI：`uv sync --frozen --all-groups` → ruff check → format check → compileall → pytest → `uv build --no-config` |
| `README.MD` | 新增“许可证”小节 |
| `tests/test_package_layout.py` | 回归：许可证存在且元数据一致、CI 覆盖必需命令、与沙箱工作流分工 |

关键设计：

- CI 仅在 `main`/`optimize` 推送和所有 PR 触发，`permissions: contents: read`，action 固定到与
  `sandbox.yml` 相同的提交 SHA，Python 3.12，uv 固定版本 `0.12.10`。
- 通用 CI 与沙箱工作流职责不重叠：通用 CI 跑工程回归与构建，沙箱工作流跑需要 Docker 的攻击
  评测。

## 验证

```text
pytest（脱敏套件，deselect 4 项沙箱权限用例）   788 passed, 2 skipped, 4 deselected
ruff check src tests                            All checks passed
ruff format --check src tests                   141 files already formatted
python -m compileall -q src tests               passed
uv build --no-config                            wheel 含 LICENSE，元数据 License-Expression: MIT / License-File: LICENSE
```

RED 证据：先加 3 项回归测试，实现前 `3 failed`（缺 `LICENSE`、缺 `ci.yml`）；实现后全部 GREEN。

## 尚未完成

- 推送后在 GitHub 上确认 `ci` 工作流首次运行通过（本地无法触发 runner）；
- 真实 90-trial 回归与 holdout（需代码冻结后运行）；
- 阶段 6 发布（版本标签、变更日志、端到端演示）。
