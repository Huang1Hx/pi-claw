# Iteration 006：业务模块直接放在 src 下

## 状态与基线

日期：2026-09-11。状态：`待用户审核`。分支：`refactor/package-layout`。

在 Iteration 004、005 尚未提交的工作区上继续调整，基线为 278 项测试通过。
用户要求项目目录为 `pi-claw/src/core`、`pi-claw/src/interfaces` 等，去掉
`src/pi_claw/` 中间层；文档、测试、运行报告和配置都在现有项目根目录。
本轮不修改模型路由策略、权限审批规则或会话数据格式，不调用真实模型 API。

## 最终目录

```text
pi-claw/
├── README.MD
├── .env                    # 本机文件，不进入 Git 或安装包
├── .env.example
├── pi-claw.example.toml
├── pyproject.toml
├── uv.lock
├── src/
│   ├── core/
│   ├── interfaces/
│   ├── tools/
│   ├── commands/
│   ├── config/
│   ├── routing/
│   ├── security/
│   ├── providers/
│   ├── persistence/
│   └── evaluation/
├── tests/
├── docs/
└── runs/                   # 保留 .gitkeep，报告忽略跟踪
```

这里的 `pi-claw/` 就是当前 Git/uv 项目根目录，没有创建第二层同名目录。
`src/` 是源码根；Python 的十个业务包直接位于其中，不使用带连字符的 Python 导入名。

## 修改清单

| 范围 | 旧位置 | 新位置与调整 |
|---|---|---|
| Agent 与数据协议 | `src/pi_claw/core/` | `src/core/`；保留编排、上下文、完成验收、事件、Hooks、Token 统计 |
| 界面 | `src/pi_claw/interfaces/` | `src/interfaces/`；保留 CLI 和 REPL |
| 模型工具 | `src/pi_claw/tools/` | `src/tools/`；保留 read/write/edit/grep/shell 和统一审批执行入口 |
| 用户命令 | `src/pi_claw/commands/` | `src/commands/`；保留 status/model/exit/sessions/help 等显式命令 |
| 配置与路由 | `src/pi_claw/config/`、`routing/` | `src/config/`、`src/routing/`；环境配置、意图规则和成本统计行为不变 |
| 权限、Provider、存储 | `src/pi_claw/security/`、`providers/`、`persistence/` | 对应迁到 `src/`，保留协议、审批和锁的原有职责 |
| 评测 | `src/pi_claw/evaluation/` | `src/evaluation/`；资源读取改为 `files("evaluation")` |
| Python 启动入口 | `src/pi_claw/__main__.py` | `src/interfaces/__main__.py`；使用标准 main guard |
| 旧根包 | `src/pi_claw/__init__.py` | 移除转导出层；调用方直接从业务模块导入 |
| 构建与安装 | `pyproject.toml` | wheel 明确打包十个 `src/<模块>`；命令入口改为 `interfaces.cli:main` |
| 项目说明 | `README.md` | 改名为 `README.MD`；同步元数据、目录树和启动方法 |
| 项目入口发现 | `src/core/prompts.py` | 支持 `.MD` 扩展名，按文件身份去重，防止 macOS 重复注入同一 README |
| 原有测试 | `tests/test_*.py` | 同步导入、mock 路径和 subprocess 的模块入口 |
| 新增目录测试 | `tests/test_package_layout.py` | 验证目录/打包清单、实际导入路径、已安装入口元数据、项目外启动 |
| 新增读取测试 | `tests/test_prompts.py` | 大写扩展名 README 可读取且只注入一次 |
| 当前文档 | `ARCHITECTURE.md`、`CONFIGURATION_AND_ROUTING.md`、`WORKFLOW_AND_DATA_FLOW.md`、`EVALUATION.md`、`RUNTIME.md` | 同步路径、示例导入和兼容性说明 |

迁移保留已有中文模块、类和函数注释；新入口、去重逻辑和新增测试补充中文说明。
本机 `.env` 原样保留，权限为 0600，仍由 Git 忽略；没有读取、覆盖或记录其中密钥。

## 导入和启动变化

```python
from core.agent import Agent
from core.schemas import ToolCall
from security.policy import WorkspacePolicy
from tools import ToolExecutor
```

```bash
# 在 pi-claw 项目根目录重新安装开发包，刷新入口和源码映射。
uv sync --frozen

# 用户命令不变。
uv run --frozen pi-claw --help

# Python 模块入口已迁移。
uv run --frozen python -m interfaces --help
```

旧 `from pi_claw ...` 和 `python -m pi_claw` 不再提供。工具协议名、持久化会话目录、
journal/checkpoint、审批行为、模型配置和路由语义均未改变，因此不需要迁移用户会话。
按要求采用顶层业务包后，应使用本项目独立的 uv 虚拟环境，避免同一环境中其他项目的
`core`、`config`、`tools` 等同名包冲突。wheel 文件名中的 `pi_claw` 是 Python 发行包名
规范化结果，不代表源码中仍有同名目录，也不改变 `pi-claw` 命令名。

## 清理与保留

旧 `src/pi_claw/` 中剩余的 `.pyc`、`__pycache__` 和 `.DS_Store` 已核实不含源码，
整体移到仓库外 `/Users/huanghx/project/pi-claw-materials/archive/iteration-006-pi-claw-cache/`，
可以恢复，不做不可恢复删除。项目根目录没有重新创建 `tmp/` 或 `fixtures/`。
课程和面试资料继续保留在 Git 仓库外；Iteration 001–005 不改写历史路径。
原先 `uv.lock` 的镜像地址改动予以保留，本轮没有修改依赖版本或新增依赖。

## 验证命令与结果

```bash
uv sync --frozen --no-config
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen python -m compileall -q src
uv run --frozen pytest --cov=src --cov-report=term-missing
uv run --frozen pi-claw eval --workers 4 --output runs/iteration-006-eval.json
uv run --frozen pi-claw eval-routing --output runs/iteration-006-routing-eval.json
uv lock --check --no-config
uv build --no-config
git diff --check
```

- 全量测试：`283 passed`，比基线增加 5 项；语句覆盖率 `86%`。
- 工程离线评测：`8/8`；最大并发 `4`，隔离违规 `0`，安全场景通过率 `100%`。
- 路由离线合约：`6/6`。假设单价与固定替身下成本算术差异仍为 `23.3%`，不是实际模型节省率。
- Ruff 格式与静态检查、Python 编译、uv 锁检查、diff 空白检查通过。
- wheel/sdist 构建成功。wheel 包含十个直接业务包、模块入口和 `evaluation/cases.json`；
  sdist 包含 `README.MD` 和两个配置模板；两者不含真实 `.env`、本机 `pi-claw.toml`、
  旧 `src/pi_claw`、缓存、课程 PDF 或面试材料。
- 在项目外临时目录中新建独立 uv 环境，仅安装 wheel：`pi-claw --help`、
  `python -m interfaces --help`、工程 Eval `8/8`、路由 Eval `6/6` 均通过。

开发环境评测报告位于 `runs/iteration-006-eval.json` 与
`runs/iteration-006-routing-eval.json`，均忽略 Git 跟踪。
本轮验证不证明真实模型质量、真实服务账单节省、远端 API 连通性或 OS 级沙箱隔离。

## Git 与后续

所有改动留在 `refactor/package-layout` 等待用户审核，未 commit、未合并 main、未 push。
README 的大小写重命名使用 Git 记录，以确保 macOS 与大小写敏感系统获得相同文件名。
用户配置 API Key 后再按需执行真实模型验证，不在本轮调用付费服务。
