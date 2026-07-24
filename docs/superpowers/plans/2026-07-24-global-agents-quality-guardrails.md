# Global AGENTS Quality Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生成一份语言无关、可直接追加到全局 `AGENTS.md` 的质量护栏文件，减少遗漏入口、假回归测试和无证据完成声明。

**Architecture:** 交付文件是独立 Markdown 章节，不覆盖现有全局规则。内容按任务分级、修改前分析、测试先行、实现约束、完成前验证、完成报告和 Token 控制组织。

**Tech Stack:** Markdown、Git、PowerShell 7。

## Global Constraints

- 规则必须适用于所有项目和编程语言。
- 不得覆盖已有 Shell、Git、权限或项目偏好。
- 简单改字、注释和纯格式调整允许轻量流程。
- 任何运行时行为变化必须使用完整流程。
- 禁止复制生产逻辑、核心行为被 Mock 后自证正确以及依赖本机真实配置的测试。
- 最终文件只作为可追加内容交付，不直接修改用户全局 `AGENTS.md`。

---

### Task 1: 生成并验证全局质量护栏追加文件

**Files:**
- Create: `docs/global-agents-quality-guardrails.md`
- Verify: `docs/superpowers/specs/2026-07-24-global-agents-quality-guardrails-design.md`

**Interfaces:**
- Consumes: 已确认设计中的任务分级、完整流程、Token 控制和验收标准。
- Produces: 可复制追加到任意全局 `AGENTS.md` 的独立 Markdown 章节。

- [ ] **Step 1: 创建追加文件**

文件必须包含以下完整章节：

```markdown
# Engineering Quality Guardrails

## Scope and task classification

Classify work before editing:

- **Lightweight task:** wording, comments, formatting, or mechanical changes that cannot alter runtime behavior.
- **Behavioral task:** any feature, bug fix, refactor, configuration change, state transition, error handling, concurrency, persistence, or external interaction.

Use the lightweight workflow only for genuinely non-behavioral changes. Behavioral tasks must follow every section below.

## Before implementation

For behavioral tasks, write a concise impact map before editing:

- User-visible goal and explicit non-goals.
- Every caller, command, route, job, callback, CLI, and background entry point.
- Success, failure, timeout, retry, same-value retry, duplicate callback, concurrency, and process-restart states.
- Relationships between disk, database, cache, remote state, and in-memory state.
- User notifications for success, failure, and indeterminate outcomes.

Search all references to affected functions, exceptions, state fields, and messages. Do not inspect only the function named in the request.

## Test-first requirements

For bug fixes, establish a failing regression test that reproduces the original symptom before implementing the fix. For new behavior, define executable acceptance tests first.

A valid regression test must:

- Invoke the real production entry point or a production helper shared by that entry point.
- Fail before the fix and pass after it.
- Assert the final state or user-visible result, not merely that a mock was called.
- Isolate credentials, configuration, time, network, database, and working-directory dependencies.
- Cover the main path and the important counterexample.

Never:

- Copy production branches into a test and test the copy.
- Mock the core behavior being verified and then use the mock result as proof.
- Depend on a developer machine's real config, cookies, database, credentials, or current working directory.
- Treat test count or a green suite as proof of coverage quality.

## Implementation rules

- Fix the root cause and make all relevant entry points reuse the same behavior.
- When adding an exception subtype, inspect every parent-class catch and catch order.
- Give locks, transactions, resources, callbacks, and cleanup steps explicit ownership.
- Never report an unknown or unverified state as confirmed success or confirmed failure.
- If rollback, refresh, notification, or cleanup can fail, handle and test that failure explicitly.
- Never log passwords, cookies, tokens, keys, secrets, or complete credentials.
- Do not use process restart to hide a runtime synchronization defect.

## Required verification

Before claiming completion, run and inspect:

- The regression or acceptance tests for the requested behavior.
- The affected module or subsystem tests.
- The complete project test suite.
- Compilation, type checking, linting, or the project's equivalent static checks.
- Diff and status checks for secrets, production data, generated files, and unrelated changes.
- Critical tests from a clean temporary directory or otherwise isolated environment.

For stateful or high-risk changes, also verify:

- Repeating the same input after the first failure.
- Success followed by refresh, notification, rollback, or cleanup failure.
- Concurrent commands and duplicate callbacks.
- A process restart between persistence and in-memory synchronization.
- Resource cleanup itself throwing an exception.

## Completion gate

Do not claim completion solely because tests are green.

The final report must state:

- Which entry points and states were covered.
- Exact verification commands and their results.
- Whether isolated-environment verification was run.
- Remaining assumptions, untested paths, and residual risks.
- Whether files were changed, committed, pushed, or deployed.

If any Critical or Important issue remains, report the work as incomplete.

## Token-efficiency rules

- Build the caller and state matrix once before editing to avoid repeated patch rounds.
- Inspect independent entry points in parallel when safe.
- Batch related tests while preserving failure attribution.
- Reuse trustworthy tests, but inspect what they actually prove.
- Use the full workflow only for behavioral tasks.
- Prefer read-only discovery over asking the user for information available in the workspace.
```

- [ ] **Step 2: 自检文本**

Run:

```powershell
pwsh -NoLogo -NoProfile -Command 'rg -n "T[B]D|T[O]DO|PLACEHOLD[E]R|implement[ ]later|fill[ ]in" docs/global-agents-quality-guardrails.md; git diff --check -- docs/global-agents-quality-guardrails.md'
```

Expected:

- 占位符搜索无匹配。
- `git diff --check` 无输出。

- [ ] **Step 3: 验证内容覆盖**

Run:

```powershell
pwsh -NoLogo -NoProfile -Command 'rg -n "^#|real production entry point|Copy production branches|clean temporary directory|Critical or Important|Token-efficiency" docs/global-agents-quality-guardrails.md'
```

Expected:

- 输出任务分级、真实入口测试、禁止假回归、隔离验证、完成门槛和 Token 控制对应行。

- [ ] **Step 4: 提交交付文件**

```powershell
git add -- docs/global-agents-quality-guardrails.md
git commit -m "docs: add global agent quality guardrails"
```

Expected: 创建一个仅包含交付 Markdown 的提交。
