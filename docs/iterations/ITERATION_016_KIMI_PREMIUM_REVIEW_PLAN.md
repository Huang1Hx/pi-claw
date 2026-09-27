# DeepSeek Default and Kimi Premium Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove GLM from current runtime roles, route normal work through `deepseek-flash`, and call the independently authenticated `kimi-k3` only for bounded high-risk read-only review.

**Architecture:** Extend model profiles with environment-backed independent connections and per-profile retry limits. Keep the existing plan-code-review state machine, then insert one optional premium review stage selected by a pure deterministic risk policy after normal review succeeds. Reuse existing provider, permission, usage, session, and trace boundaries.

**Tech Stack:** Python 3.12, dataclasses, TOML, uv, pytest, ruff, OpenAI-compatible Chat Completions.

**Spec:** `docs/iterations/ITERATION_016_KIMI_PREMIUM_REVIEW.md`

## Global Constraints

- All normal roles use the school `deepseek-flash` connection; no current automatic role may request GLM.
- Kimi uses `MOONSHOT_BASE_URL`, `MOONSHOT_API_KEY`, exact model ID `kimi-k3`, and one HTTP attempt per logical request.
- Kimi receives only read tools and never replaces host verification.
- Kimi is not called until normal review and write-verification evidence have passed.
- Secrets stay out of tracked files, repr, CLI output, Session, and Trace.
- Preserve all pre-existing uncommitted evaluation changes; do not commit or push until the user reviews the final diff.
- Run only one paid Kimi smoke request; do not live-test the full automatic premium workflow.

## Review Focus

- A profile that sets only `base_url_env` or only `api_key_env` must fail before networking instead of inheriting the school credential.
- A high-risk task with no changed file must not spend Kimi merely because it contains a security word.
- A normal reviewer approval that enters premium review must count its usage once, not lose or duplicate it.
- Kimi tool calls must be read-only and must reread every changed file in the premium phase even if normal review already read it.
- Kimi rejection or provider failure must never produce `completed` or silently fall back to DeepSeek approval.

---

### Task 1: Independent profile connection and retry limit

**Files:**
- Modify: `src/config/models.py`
- Modify: `src/config/loader.py`
- Modify: `src/routing/router.py`
- Test: `tests/test_shared_model_connection.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ModelProfile.max_attempts: int | None`.
- Produces: model profile support for paired `base_url_env` and `api_key_env`.
- Consumes: existing `resolve_connection(value, environment, label=...)`.

- [x] **Step 1: Write failing connection and retry tests**

Add behavior tests proving that a model profile resolves `base_url_env` with its own key, rejects either half of the pair, redacts credential values from errors/repr, and validates `max_attempts` in the inclusive range 1–10.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen pytest tests/test_shared_model_connection.py tests/test_config.py -q`

Expected: failures because model profiles currently reject `base_url_env` and `max_attempts`.

- [x] **Step 3: Implement the configuration contract**

Add `max_attempts: int | None = None` to `ModelProfile`. In `load_settings()`, admit `base_url_env` and `max_attempts`, pass all present connection fields to `resolve_connection()`, and validate the retry limit without echoing values. In `RoutedProvider._request()`, pass `profile.max_attempts` when set and otherwise use the CLI/global value.

- [x] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --frozen pytest tests/test_shared_model_connection.py tests/test_config.py -q`

Expected: all selected tests pass.

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- src/config/models.py src/config/loader.py src/routing/router.py tests/test_shared_model_connection.py tests/test_config.py`

### Task 2: Deterministic premium-review policy

**Files:**
- Create: `src/routing/premium_review.py`
- Create: `tests/test_premium_review.py`
- Modify: `src/config/models.py`
- Modify: `src/config/loader.py`

**Interfaces:**
- Produces: `premium_review_reason(task: str, changed_files: Collection[str], review_rounds: int, policy: str) -> str | None`.
- Produces: `WorkflowSettings.premium_reviewer: str | None`, `premium_review_policy: str`, and `max_premium_review_requests: int`.

- [x] **Step 1: Write failing policy tests**

Use literal cases for `off`, `explicit`, and `risk`. Pin explicit phrases, high-risk task/path terms, `review_rounds >= 2`, `len(changed_files) >= 5`, and the no-changed-file guard. Add loader tests for invalid policy, request limit outside 1–12, and a missing premium profile when premium review is enabled.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen pytest tests/test_premium_review.py tests/test_config.py -q`

Expected: import/config failures because the policy and workflow fields do not exist.

- [x] **Step 3: Implement the pure policy and workflow validation**

Keep keyword/path matching in `premium_review.py`; return a stable Chinese reason for the first matched rule. `explicit` checks only explicit request phrases. `risk` checks explicit request plus risk, second-round approval, and five-file rules, but all automatic rules require at least one changed file. Validate the premium profile only when policy is not `off`.

- [x] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --frozen pytest tests/test_premium_review.py tests/test_config.py -q`

Expected: all selected tests pass.

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- src/routing/premium_review.py src/config/models.py src/config/loader.py tests/test_premium_review.py tests/test_config.py`

### Task 3: Premium read-only workflow stage

**Files:**
- Modify: `src/routing/workflow.py`
- Modify: `src/routing/workflow_protocol.py`
- Modify: `src/routing/router.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: `premium_review_reason(...)` and new workflow settings from Task 2.
- Produces: phases `premium_reviewing` and `premium_review_approved`.
- Produces: workflow summary fields `premium_review_triggered`, `premium_review_reason`, `premium_review`, and `premium_reviewed_files`.

- [x] **Step 1: Write failing workflow tests**

Add tests proving: low-risk approval returns without Kimi; explicit/high-risk/two-round/five-file cases enter Kimi once; normal approval usage is emitted before switching; Kimi rereads every changed file; write/command tools are blocked; Kimi approval reaches the outer verifier; Kimi rejection and request-cap exhaustion stop unverified.

- [x] **Step 2: Run focused workflow tests and verify RED**

Run: `uv run --frozen pytest tests/test_workflow.py -q`

Expected: failures because the premium phase and summary fields do not exist.

- [x] **Step 3: Implement the premium phase**

Add a stricter premium prompt that treats the normal review as untrusted evidence. After normal approval, evaluate the pure policy; if it does not trigger, preserve current final-response behavior. If it triggers, emit normal-review usage exactly once, start a new private premium conversation, clear premium read evidence, expose only read tools, enforce `max_premium_review_requests`, and require complete rereads. Premium approval returns the candidate with both review summaries; premium rejection raises `WorkflowStopped` after persisting its artifact and usage.

- [x] **Step 4: Run focused workflow tests and verify GREEN**

Run: `uv run --frozen pytest tests/test_workflow.py -q`

Expected: all selected tests pass.

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- src/routing/workflow.py src/routing/workflow_protocol.py src/routing/router.py tests/test_workflow.py`

### Task 4: CLI visibility and model transport

**Files:**
- Modify: `src/commands/model.py`
- Modify: `src/interfaces/repl.py`
- Modify: `tests/test_command_dashboard.py`
- Modify: `tests/test_workflow_transport.py`

**Interfaces:**
- Consumes: premium workflow settings and stages from Tasks 2–3.
- Produces: `/model` role label `高级审查` and human-readable premium workflow events.

- [x] **Step 1: Write failing CLI and transport tests**

Assert `/model` displays `kimi-k3 [premium_reviewer]` separately from all DeepSeek roles, premium stages render once, normal school requests use the school connection, and the premium request uses the Moonshot connection with `max_attempts=1`. Assert neither credential appears in output or object repr.

- [x] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/test_command_dashboard.py tests/test_workflow_transport.py -q`

Expected: failures because premium role/stage rendering and transport selection are absent.

- [x] **Step 3: Implement CLI visibility**

Extend only the existing role and phase displays. Do not print URLs or keys. Keep fixed profile selection behavior unchanged; `/model premium_reviewer` remains an explicit one-model diagnostic path and bypasses automatic workflow like other fixed profiles.

- [x] **Step 4: Run focused tests and verify GREEN**

Run: `uv run --frozen pytest tests/test_command_dashboard.py tests/test_workflow_transport.py -q`

Expected: all selected tests pass.

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- src/commands/model.py src/interfaces/repl.py tests/test_command_dashboard.py tests/test_workflow_transport.py`

### Task 5: Runtime templates, local credentials, and current documentation

**Files:**
- Modify: `.env.example`
- Modify: `pi-claw.example.toml`
- Modify: `pi-claw.ustc.example.toml`
- Modify ignored local files: `.env`, `pi-claw.toml`
- Modify: `README.MD`
- Modify: `docs/CONFIGURATION_AND_ROUTING.md`
- Modify: `docs/WORKFLOW_PIPELINE.md`
- Modify: `docs/AGENT_MODE_COMPARISON.md`
- Modify: `docs/iterations/ITERATION_015_AGENT_MODE_COMPARISON.md`
- Modify: `docs/iterations/README.md`
- Test: `tests/test_workflow_transport.py`

**Interfaces:**
- Consumes: exact local values from `/Users/huanghx/API_key/kimi.txt` without printing them.
- Produces: four DeepSeek normal profiles and one Kimi premium profile in the school/local templates.

- [x] **Step 1: Extend the end-to-end template test and verify RED**

Assert all normal role models equal `deepseek-flash`, the premium role equals `kimi-k3`, normal profiles share the school key, the premium profile uses a different fake Moonshot key/URL, and no automatic low-risk request reaches Kimi.

- [x] **Step 2: Run the transport test and verify RED**

Run: `uv run --frozen pytest tests/test_workflow_transport.py -q`

Expected: template assertions fail against the current GLM roles and absent Kimi profile.

- [x] **Step 3: Update templates and synchronize local ignored files**

Use `deepseek-flash` for `economy`, `planner`, `primary`, and `reviewer`; keep thinking parameters only where the school endpoint has already been validated. Add `premium_reviewer` with the exact Kimi model and independent environment connection. Parse and write `MOONSHOT_BASE_URL`/`MOONSHOT_API_KEY` from the credential file without echoing values; enforce `.env` mode 0600.

- [x] **Step 4: Update current product documentation**

Describe the final DeepSeek/Kimi flow, deterministic trigger rules, paid-call boundary, token accounting, and failure semantics. Preserve historical GLM failure evidence while marking it superseded by Iteration 016.

- [x] **Step 5: Run template tests and secret scans**

Run: `uv run --frozen pytest tests/test_workflow_transport.py -q`

Run a credential-safe comparison that checks the local variables equal the source values and prints only boolean results. Then run the existing trace-redaction tests and confirm tracked diffs contain neither source key nor `.env` values.

Expected: template test passes; both boolean comparisons are true; secret scan reports no tracked exposure.

### Task 6: Full offline verification

**Files:**
- Verify only; fix failures in the owning task's files using a new RED/GREEN cycle.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: fresh complete test and lint evidence.

- [x] **Step 1: Run the complete test suite**

Run: `uv run --frozen pytest`

Expected: zero failures. If any test fails, report it by name and return to the owning task before proceeding.

- [x] **Step 2: Run lint**

Run: `uv run --frozen ruff check .`

Expected: exit 0 with no lint errors.

- [x] **Step 3: Check repository and secret boundaries**

Run: `git diff --check`, inspect `git status --short`, confirm `.env` and `pi-claw.toml` remain ignored, and inspect the full diff while preserving pre-existing evaluation edits.

Expected: no whitespace errors, no secret-bearing file tracked, and no unrelated modification overwritten.

### Task 7: Minimal live provider validation

**Files:**
- Runtime output only under ignored `runs/`; no source changes unless a reproducible failure first receives a regression test.

**Interfaces:**
- Consumes: validated `pi-claw.toml` and `.env`.
- Produces: one DeepSeek smoke result and at most one Kimi paid smoke result.

- [x] **Step 1: Run the free DeepSeek smoke**

Run: `uv run --frozen pi-claw run "不要调用工具，只回复 READY" --workspace projects --config pi-claw.toml --route primary --max-steps 2 --trace runs/deepseek-model-smoke.jsonl`

Expected: non-empty `READY` response from model `deepseek-flash`, no GLM model ID in the trace, and recorded/unknown usage reported honestly.

- [x] **Step 2: Run exactly one paid Kimi smoke**

Run: `uv run --frozen pi-claw run "不要调用工具，只回复 READY" --workspace projects --config pi-claw.toml --route premium_reviewer --max-steps 1 --trace runs/kimi-premium-smoke.jsonl`

Expected: one logical request using model `kimi-k3`; success yields non-empty `READY`. On failure, stop without retrying or issuing another Kimi request and report the sanitized provider error.

- [x] **Step 3: Inspect sanitized evidence and final diff**

Verify trace/model/request counts without printing Authorization data, rerun the full suite if live diagnosis required code changes, and report exact test counts plus the live outcomes. Do not commit, push, or merge.
