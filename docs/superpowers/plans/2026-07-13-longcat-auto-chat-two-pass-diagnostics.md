# LongCat Auto Chat and Two-Pass Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route ordinary non-command questions into LongCat chat automatically and replace iterative diagnostic JSON exchanges with one planning call, bounded read-only evidence collection, and one Markdown report call.

**Architecture:** Existing deterministic commands remain first and never call LongCat. Unknown text is classified as command, diagnose, chat, or clarify; chat reuses the current per-user chat service. Diagnostics use exactly two model stages around at most three validated read-only tool calls, retaining the shared command lock and 30-second deadline.

**Tech Stack:** Python 3.12, Flask callback bridge, requests-based LongCat client, pytest, systemd deployment to the Seoul server.

---

### Task 0: Prioritize NAV Prediction Language

**Files:**
- Modify: `src/nav_monitor.py`
- Test: `tests/test_nav_monitor_commands.py`

- [ ] **Step 1: Write failing prediction-language tests**

Assert that “今天我的理财能涨吗”, “我的理财会不会跌” and “帮我预测一下理财涨跌” produce `NavCommand(action="estimate_holdings")`, while “查询今天理财净值” remains a latest-value query.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_nav_monitor_commands.py -q`

Expected: “今天我的理财能涨吗” is currently parsed as `query_latest`.

- [ ] **Step 3: Implement prediction-first parsing**

Add a focused prediction-language predicate for combinations such as `能涨`, `会涨`, `会跌`, `涨不涨`, `预测`, `预估` and `走势`, and return `estimate_holdings` before period or date query parsing. Preserve an explicit date when the parser already supports it.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_nav_monitor_commands.py -q`

Expected: prediction questions map to `estimate_holdings` and explicit net-value queries remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/nav_monitor.py tests/test_nav_monitor.py docs/superpowers/specs/2026-07-13-longcat-auto-chat-two-pass-diagnostics-design.md docs/superpowers/plans/2026-07-13-longcat-auto-chat-two-pass-diagnostics.md
git commit -m "fix: prioritize NAV prediction questions"
```

### Task 1: Route Ordinary Questions to Chat

**Files:**
- Modify: `src/ai_command_router.py`
- Test: `tests/test_ai_command_router.py`

- [ ] **Step 1: Write failing router tests**

Add stub responses for `kind=chat`, assert the route is accepted, and assert the system prompt explicitly maps ordinary knowledge, weather, and conversation questions to `chat` while reserving `clarify` for ambiguous system operations.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_ai_command_router.py -q`

Expected: failure because `chat` is not an accepted route kind.

- [ ] **Step 3: Implement the route contract**

Extend the accepted kinds to `{"command", "diagnose", "chat", "clarify"}`. For `chat`, return `AiRoute(kind="chat", confidence=..., reply=...)`. Update the prompt with examples including a typhoon question mapped to chat and a missing product-code operation mapped to clarify.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_ai_command_router.py -q`

Expected: all router tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_command_router.py tests/test_ai_command_router.py
git commit -m "feat: route ordinary questions to chat"
```

### Task 2: Reuse Chat Execution for Automatic Fallback

**Files:**
- Modify: `src/ai_assistant.py`
- Test: `tests/test_server_ai_integration.py`

- [ ] **Step 1: Write failing bridge tests**

Create a fake `chat` route and assert that an unprefixed ordinary question calls `ask_chat`, sends the LongCat waiting message and answer, releases the already-held command lock, and does not request a second lock. Add an async-start failure assertion to prove the lock is released.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_server_ai_integration.py -q`

Expected: the bridge does not handle `route.kind == "chat"`.

- [ ] **Step 3: Extract one lock-aware chat launcher**

Add a private bridge method that assumes the command lock is already held, sends the waiting message, starts the worker, sends the answer, and releases the lock in `finally`. Use it both after explicit chat acquires the lock and after the router returns `chat` while retaining its existing lock.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_server_ai_integration.py -q`

Expected: automatic and explicit chat tests pass, including lock cleanup.

- [ ] **Step 5: Commit**

```bash
git add src/ai_assistant.py tests/test_server_ai_integration.py
git commit -m "feat: add automatic LongCat chat fallback"
```

### Task 3: Replace Iterative Diagnostics with Two Passes

**Files:**
- Modify: `src/ai_diagnostic_agent.py`
- Modify: `src/ai_diagnostic_tools.py`
- Test: `tests/test_ai_diagnostic_agent.py`
- Test: `tests/test_ai_diagnostic_tools.py`

- [ ] **Step 1: Write failing two-pass tests**

Replace iterative-agent expectations with a plan response such as:

```json
{"tools":[{"tool":"get_schedule_snapshot","arguments":{}},{"tool":"search_logs","arguments":{"query":"nav monitor","since_minutes":1440,"limit":80}}]}
```

Assert exactly two model calls, planning with thinking enabled, final reporting with thinking disabled, at most three deduplicated tools, no execution for unknown tools or malformed plans, a shared 30-second deadline, and final Markdown redaction.

- [ ] **Step 2: Run diagnostic tests and confirm RED**

Run: `python -m pytest tests/test_ai_diagnostic_agent.py tests/test_ai_diagnostic_tools.py -q`

Expected: current iterative agent makes additional model calls and lacks an exposed tool allowlist.

- [ ] **Step 3: Expose the toolbox allowlist**

Add `DiagnosticToolbox.allowed_tools()` returning a frozen set containing only `get_schedule_snapshot`, `get_service_snapshot`, `search_logs`, `search_source`, and `read_source`. Reuse this set in `execute` so validation and execution cannot drift.

- [ ] **Step 4: Implement planning validation**

Parse one JSON object with a `tools` list, require string tool names and object arguments, reject unknown tools before any execution, deduplicate identical calls, and keep the first three validated calls.

- [ ] **Step 5: Implement bounded evidence collection and reporting**

Execute the plan, convert tool exceptions into redacted failure evidence, then call LongCat once with the original question and evidence JSON. Request Markdown containing `原因`, `诊断依据`, `建议处理`, and `置信度`; reject missing sections, redact again, and truncate to 3500 characters. Enforce the 30-second deadline before each phase and use the remaining duration for the final request.

- [ ] **Step 6: Run diagnostic tests and confirm GREEN**

Run: `python -m pytest tests/test_ai_diagnostic_agent.py tests/test_ai_diagnostic_tools.py -q`

Expected: all two-pass, allowlist, deadline, and redaction tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/ai_diagnostic_agent.py src/ai_diagnostic_tools.py tests/test_ai_diagnostic_agent.py tests/test_ai_diagnostic_tools.py
git commit -m "refactor: use two-pass LongCat diagnostics"
```

### Task 4: Update User Documentation and Run Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-13-longcat-assistant-design.md`

- [ ] **Step 1: Document automatic chat and two-pass diagnosis**

State that plain non-command questions automatically chat, explicit chat commands remain supported, diagnosis performs one plan and one report call, at most three read-only tools are executed, and the 30-second total limit remains.

- [ ] **Step 2: Run full verification**

Run:

```bash
python -m pytest -q
python -m compileall -q src tests
git diff --check
```

Expected: all tests pass, compilation exits 0, and diff check is clean.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-13-longcat-assistant-design.md
git commit -m "docs: update LongCat chat and diagnosis behavior"
```

### Task 5: Production Verification and Deployment

**Files:**
- Deploy: `src/ai_command_router.py`
- Deploy: `src/ai_assistant.py`
- Deploy: `src/ai_diagnostic_agent.py`
- Deploy: `src/ai_diagnostic_tools.py`

- [ ] **Step 1: Run a deployment dry run**

Use the Seoul deployment skill with the four exact runtime files and no configuration or data files. Confirm the printed backup directory and successful local tests.

- [ ] **Step 2: Execute the scoped deployment**

Run the same deployment command with `-Execute`. Require upload, remote compile, restart, HTTP 200, and SHA256 checks to pass.

- [ ] **Step 3: Verify real automatic chat**

Call the production router with a normal typhoon question and confirm `kind=chat`; call the production chat service and confirm a nonempty answer without the `问助手` prefix.

- [ ] **Step 4: Verify real two-pass diagnosis**

Run one production read-only diagnostic for “为什么今天没有自动发送理财净值日报”. Confirm one plan call, no more than three tools, one Markdown report call, nonempty redacted output, and completion within 30 seconds.

- [ ] **Step 5: Report deployment evidence**

Report test count, deployed files, backup path, service state, HTTP status, hash results, and any remaining model-output risk without exposing the API key.
