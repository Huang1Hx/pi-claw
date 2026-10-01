# Trace-Guided Evaluation Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 合并阶段二的补跑结果，并让评测系统根据脱敏 Trace 自动修复候选代码，保留首次结果与完整成本账本。

**Architecture:** `mode_reports.py` 负责纯离线身份校验、来源指纹、子集合并和聚合元数据；`mode_repair.py` 负责选择修复项、生成脱敏证据、准备干净工作区和计算修复指标。`compare_modes.py` 只负责编排 CLI、子进程和已有评分入口，避免继续扩大单文件职责。

**Tech Stack:** Python 3.12、pytest、uv、现有 JSON/JSONL Trace 与 `evaluation.mode_grading`。

**Spec:** `docs/superpowers/specs/2026-09-29-trace-guided-eval-repair-design.md`

## Global Constraints

- 来源报告、trial 工作区和 Trace 只读，所有结果写入不存在的新目录。
- 自动修复只能编辑候选 `solution.py`，不得编辑评分器、隐藏用例、任务规格或产品源码。
- `provider_error` 只走恢复批次，不进入语义修复。
- Trace 证据不得包含隐藏 expected/actual、API Key、模型 URL 或用户绝对路径。
- 每个失败最多两轮，仍使用 420 秒 trial 上限与现有有界 HTTP 重试。
- 首次成功率、修复后成功率和额外采集成本必须分开报告。
- 本轮不自动 commit 或 push；用户审核后再决定提交。

## Review Focus

- 替换报告含重复或来源之外身份时必须失败，不能静默丢行。
- 来源工作区缺失时必须报告具体身份，不能沿用旧评分结果伪装成当前口径。
- Trace 中含绝对路径、expected/actual 或连接信息时必须脱敏且限长。
- 上一轮污染 `SPEC.md`/`test_target.py` 时，新工作区必须恢复任务目录原件。
- 修复轮出现 provider error 或 Token 缺失时，汇总必须保留未知量与失败状态。

---

### Task 1: Immutable subset merge

**Files:**
- Create: `src/evaluation/mode_reports.py`
- Create: `tests/test_mode_reports.py`
- Modify: `src/evaluation/compare_modes.py`

**Interfaces:**
- Produces: `trial_identity(row) -> tuple[str, str, int]`
- Produces: `load_report_root(root: Path) -> tuple[dict, Path, str]`
- Produces: `merge_subset(source, replacement, *, regrade) -> tuple[list[dict], dict]`
- Produces: `validate_compatible_runs(source, replacement) -> None`
- Consumes: `build_trial_report(folder, metadata)` and `summarize(rows, premium_model=...)`

- [ ] Write tests for exact subset replacement, current-grader regrading, duplicate/unknown identity rejection, incompatible model/budget rejection and source SHA provenance.
- [ ] Run `uv run --frozen pytest tests/test_mode_reports.py -q` and observe failures because the module/CLI is absent.
- [ ] Implement `mode_reports.py` and add offline `--merge-from`/`--replace-from` CLI paths without loading model settings.
- [ ] Run the focused tests and the existing comparison tests; expect all to pass.

### Task 2: Sanitized evidence and clean repair workspaces

**Files:**
- Create: `src/evaluation/mode_repair.py`
- Create: `tests/test_mode_repair.py`

**Interfaces:**
- Consumes: `trial_identity(row)` and `TASKS`
- Produces: `RepairJob` dataclass
- Produces: `select_repair_jobs(report) -> list[RepairJob]`
- Produces: `build_repair_evidence(row, *, max_chars=8000) -> dict`
- Produces: `prepare_repair_workspace(job, destination: Path) -> Path`
- Produces: `build_repair_prompt(job, evidence, round_number) -> str`

- [ ] Write tests that select only eligible semantic/execution failures and reject duplicates or missing candidates.
- [ ] Write tests proving evidence retains check/layer/public errors but removes expected/actual, secrets, URLs and absolute paths within 8,000 characters.
- [ ] Write a test that mutates source fixtures and proves the new workspace restores catalog fixtures while preserving only `solution.py`.
- [ ] Run `uv run --frozen pytest tests/test_mode_repair.py -q` and observe the expected missing-feature failures.
- [ ] Implement the minimal pure functions and rerun focused tests until green.

### Task 3: Bounded repair orchestration and ledger

**Files:**
- Modify: `src/evaluation/compare_modes.py`
- Modify: `src/evaluation/mode_repair.py`
- Modify: `tests/test_mode_repair.py`
- Modify: `tests/test_agent_mode_comparison.py`

**Interfaces:**
- Consumes: Task 2 repair jobs, prompt and prepared candidate.
- Produces: `run_repair_round(job, round_number, args) -> dict`
- Produces: `summarize_repairs(baseline, ledger, *, premium_model=None) -> dict`
- CLI: `--repair-from PATH --repair-rounds {1,2}` requires `--live`.

- [ ] Write orchestration tests proving original mode/identity are preserved, the seed solution reaches the worker, success stops later rounds, and at most two rounds run.
- [ ] Write metric tests for first-pass versus post-repair success, per-round recovery, incremental known Token, acquisition cost, unresolved layers and unknown usage.
- [ ] Run focused tests and observe missing orchestration/metric failures.
- [ ] Add hidden worker prompt/seed arguments, round directories, parent hashes and the immutable `repair-ledger.json`/`report.json` writers.
- [ ] Rerun focused tests and existing evaluation tests; expect all to pass.

### Task 4: Real-data continuation, merge and repair

**Files:**
- Create: new ignored directories under `runs/`
- Modify: `docs/iterations/` only if the repository's existing iteration format requires a result record.

**Interfaces:**
- Consumes: the original 90-trial report and the dependency-order 9-trial report.
- Produces: a completed dependency continuation, merged 90-trial report and repair report with lineage.

- [ ] Rerun only the two `provider_error` dependency trials with one worker, 420-second deadline and current retry settings.
- [ ] Merge the completed 9-trial dependency report into the original 90-trial baseline offline.
- [ ] Run up to two repair rounds for all eligible merged failures, preserving each trial's original mode.
- [ ] Inspect manifests, hashes, trial counts, status distributions and Token accounting; do not present incomplete output as final.

### Task 5: Verification and review

**Files:**
- Modify: docs only if verification reveals documented behavior changed.

**Interfaces:**
- Consumes: all implementation and generated reports.
- Produces: a reviewable uncommitted diff and evidence-backed result summary.

- [ ] Run `uv run --frozen ruff format --check src tests`.
- [ ] Run `uv run --frozen ruff check src tests`.
- [ ] Run `uv run --frozen pytest` and require zero failures.
- [ ] Self-review the complete diff against the spec, because this run is not allowed to delegate to a subagent.
- [ ] Verify the original reports are byte-identical to their recorded SHA256 and list any remaining provider/model failures separately from code failures.
