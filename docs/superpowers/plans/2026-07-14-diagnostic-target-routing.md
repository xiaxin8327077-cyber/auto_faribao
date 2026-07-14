# Diagnostic Target Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent LongCat from diagnosing the statistics push when users ask why the daily report was not sent.

**Architecture:** Derive a narrow diagnostic target locally from the user question, pass it unchanged to both model phases, and make schedule labels unambiguous. Model and tool call counts remain unchanged.

**Tech Stack:** Python 3.12, pytest, existing two-pass LongCat diagnostic agent.

---

### Task 1: Pin the diagnostic target

**Files:**
- Modify: `src/ai_diagnostic_agent.py`
- Test: `tests/test_ai_diagnostic_agent.py`

- [ ] Add a failing test showing “为啥还没发日报今天” must include “日报自动提交（不是日报统计推送）” in planning and report evidence.
- [ ] Add `_diagnostic_target` rules for NAV reports, explicit statistics reports, and ordinary OA daily reports.
- [ ] Require both model prompts to preserve the locally supplied target.
- [ ] Run the focused agent tests.

### Task 2: Clarify schedule evidence

**Files:**
- Modify: `src/ai_diagnostic_tools.py`
- Test: `tests/test_ai_diagnostic_tools.py`

- [ ] Add a failing assertion for `日报统计推送（仅周日/月末）`.
- [ ] Rename the snapshot field without changing scheduler configuration.
- [ ] Run diagnostic-tool tests.

### Task 3: Verify and deploy

**Files:**
- Deploy: `src/ai_diagnostic_agent.py`
- Deploy: `src/ai_diagnostic_tools.py`

- [ ] Run full tests, Python compilation, and `git diff --check`.
- [ ] Merge to `master` and run full tests again.
- [ ] Dry-run and execute the scoped Seoul deployment.
- [ ] Verify service health, hashes, target hint, and a real two-pass diagnosis.
