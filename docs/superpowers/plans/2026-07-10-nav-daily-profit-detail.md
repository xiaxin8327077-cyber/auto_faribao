# NAV Daily Profit Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mobile bottom sheet that switches between combined profit by NAV date, natural month, and natural year when cumulative profit is selected.

**Architecture:** Extend the existing cached dashboard payload with a `daily_profits` projection grouped from immutable ledger entries. Render that projection in the existing single-page dashboard; no new endpoint, provider request, or persisted state is introduced.

**Tech Stack:** Python, Flask, vanilla HTML/CSS/JavaScript, pytest

---

### Task 1: Daily Profit Projection

**Files:**
- Modify: `src/nav_dashboard.py`
- Test: `tests/test_nav_dashboard.py`

- [ ] **Step 1: Write the failing aggregation test**

Create two products with entries on the same date and another entry on a newer date. Assert `payload()["daily_profits"]` equals a newest-first list whose same-date amounts are summed.

- [ ] **Step 2: Verify the test fails**

Run: `pytest -q tests/test_nav_dashboard.py -k daily_profit`

Expected: failure because `daily_profits` is missing.

- [ ] **Step 3: Add the minimal projection**

Group `profit_entries` by `nav_date`, `nav_date[:7]`, and `nav_date[:4]` with `Decimal`, then return daily, monthly, and yearly projections.

```python
"daily_profits": [
    {"date": nav_date, "amount": _decimal_text(amount)}
    for nav_date, amount in sorted(daily_totals.items(), reverse=True)
]
```

- [ ] **Step 4: Verify the focused test passes**

Run: `pytest -q tests/test_nav_dashboard.py -k daily_profit`

Expected: pass.

### Task 2: Mobile Bottom Sheet

**Files:**
- Modify: `src/nav_dashboard_page.html`
- Test: `tests/test_nav_dashboard_routes.py`

- [ ] **Step 1: Write the failing page-shell test**

Assert the page contains `id="dailyProfitSheet"`, `id="dailyProfitList"`, and an accessible cumulative-profit trigger.

- [ ] **Step 2: Verify the test fails**

Run: `pytest -q tests/test_nav_dashboard_routes.py -k daily_profit`

Expected: failure because the sheet is absent.

- [ ] **Step 3: Implement the sheet and interaction**

Add a fixed backdrop, bottom-aligned sheet, compact date/amount rows, close button, and JavaScript handlers for trigger click, backdrop click, close click, and Escape. Add an accessible segmented control that defaults to daily and switches to monthly or yearly cached projections without another API request.

- [ ] **Step 4: Verify focused tests pass**

Run: `pytest -q tests/test_nav_dashboard_routes.py -k daily_profit`

Expected: pass.

### Task 3: Regression, Visual QA, and Deployment

**Files:**
- Modify: `design-qa.md`

- [ ] **Step 1: Run all automated checks**

Run: `pytest -q && python -m compileall -q src && git diff --check`

Expected: all tests pass with zero compile or whitespace errors.

- [ ] **Step 2: Verify the mobile interaction**

At 390px, open the private dashboard, select cumulative profit, confirm newest-first rows and colors, scroll the list, close it through the icon and backdrop, and confirm no console errors or horizontal overflow.

- [ ] **Step 3: Back up and deploy scoped files**

Back up `src/nav_dashboard.py` and `src/nav_dashboard_page.html`, upload only those files, compile, restart `daily-report`, and verify authenticated API/page responses.
