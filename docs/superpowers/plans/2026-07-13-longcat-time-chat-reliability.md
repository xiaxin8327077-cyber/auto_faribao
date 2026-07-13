# LongCat Time and Chat Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct diagnostic time evidence and make short casual chats resilient to one transient LongCat failure.

**Architecture:** Add Beijing time directly to the read-only schedule snapshot, route a narrow greeting set directly through the existing chat path, and retry only transient chat availability failures once. Existing command locking and business handlers remain unchanged.

**Tech Stack:** Python 3.12, pytest, requests-based LongCat client, existing enterprise WeChat bridge.

---

### Task 1: Add authoritative Beijing time to diagnostics

**Files:**
- Modify: `src/ai_diagnostic_tools.py`
- Test: `tests/test_ai_diagnostic_tools.py`

- [ ] Add a failing test that injects `now_fn=lambda: datetime(2026, 7, 13, 17, 56)` and expects `当前北京时间：2026-07-13 17:56` in `get_schedule_snapshot`.
- [ ] Run `pytest -q tests/test_ai_diagnostic_tools.py::test_schedule_snapshot_includes_authoritative_beijing_time` and verify it fails because `now_fn` is unsupported.
- [ ] Add `now_fn` to `DiagnosticToolbox`, default it to `src.beijing_time.now`, and render it separately from all configured times.
- [ ] Re-run the focused diagnostic-tool tests and commit.

### Task 2: Route greetings directly to resilient chat

**Files:**
- Modify: `src/ai_assistant.py`
- Modify: `src/ai_chat.py`
- Test: `tests/test_server_ai_integration.py`
- Test: `tests/test_ai_chat.py`

- [ ] Add failing bridge tests proving “咪咪在吗” and “在嘛，咪咪” start chat without calling the router.
- [ ] Add a failing chat-service test whose client raises `LongCatUnavailableError` once and succeeds once.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Add a narrow `_looks_like_casual_chat` predicate after known-command handling and reuse `_launch_chat`.
- [ ] Retry `LongCatUnavailableError` once in `AiChatService.ask`, appending conversation history only after success.
- [ ] Re-run focused tests and commit.

### Task 3: Verify and deploy

**Files:**
- Deploy: `src/ai_assistant.py`
- Deploy: `src/ai_chat.py`
- Deploy: `src/ai_diagnostic_tools.py`

- [ ] Run `pytest -q`, `python -m compileall -q src`, and `git diff --check`.
- [ ] Merge the verified branch into `master` and re-run `pytest -q`.
- [ ] Run the Seoul deployment skill dry run, then execute the same three-file deployment.
- [ ] Verify HTTP 200, active service, matching hashes, a Beijing-time snapshot, and a real casual chat response.

### Task 4: Answer current-time questions locally

**Files:**
- Modify: `src/ai_assistant.py`
- Test: `tests/test_server_ai_integration.py`

- [ ] Add a failing bridge test for “现在几点钟”“当前时间”“几点了” that injects a fixed `now_fn`.
- [ ] Verify the test fails because `AiMessageBridge` does not yet accept `now_fn`.
- [ ] Recognize only explicit current-time phrases and return the injected Beijing time without acquiring a command lock or calling LongCat.
- [ ] Run the bridge tests, full suite, compilation, and diff checks before deploying `src/ai_assistant.py`.
