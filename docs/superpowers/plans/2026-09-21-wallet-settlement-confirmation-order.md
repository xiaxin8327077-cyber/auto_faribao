# Wallet Settlement Confirmation Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make redemption-settlement wallet purchases keep a stable intraday order across confirmation, then safely recover the production backlog.

**Architecture:** Add a narrow semantic ordering predicate in the position projector for wallet purchases created by redemption settlement. Keep realtime inclusion and validation rules unchanged, prove the regression with a production-shaped transaction sequence, and use the existing idempotent settlement cycle for recovery.

**Tech Stack:** Python 3.12, SQLite, pytest, systemd

## Global Constraints

- Do not modify or reverse the two already-confirmed source redemptions.
- Do not disable negative-share validation or catch-and-ignore settlement failures.
- Production database writes require a fresh SQLite backup and a stopped service.
- The 21 September SIP must remain pending until its own quote and confirmation date are available.
- Post-deployment database differences must be limited to the approved pending transactions, generated income rows, and rebuilt wallet/fund positions.

---

### Task 1: Reproduce the confirmation-order regression

**Files:**
- Modify: `tests/test_portfolio_transactions.py`

**Interfaces:**
- Consumes: `PortfolioTransactionService.settle_pending(as_of_date)` and `PositionProjector.calculate(product_id)`
- Produces: a regression test covering a late-created redemption-settlement wallet purchase and an earlier-created same-day outflow

- [ ] **Step 1: Write the failing test**

Create a wallet with an opening balance, a pending wallet purchase marked `created_by="redemption_settlement"` and linked to an origin redemption, and a same-day confirmed outflow. Force the wallet purchase `created_at` after the outflow, then assert `settle_pending` confirms it without changing final wallet shares.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_portfolio_transactions.py::test_redemption_settlement_wallet_purchase_keeps_priority_when_confirmed -q`

Expected: FAIL with `ValueError: negative portfolio shares`.

### Task 2: Implement stable redemption-settlement ordering

**Files:**
- Modify: `src/portfolio_positions.py`
- Test: `tests/test_portfolio_transactions.py`

**Interfaces:**
- Consumes: wallet product metadata and persisted `Transaction.created_by`, `origin_transaction_id`, `transaction_type`, and `shares`
- Produces: `redemption_settlement_purchase_is_start_of_day(product, transaction) -> bool`

- [ ] **Step 1: Add the minimal ordering predicate**

Return true only for positive-share `MANUAL_PURCHASE` transactions on the WalletPlus cash product that were created by `redemption_settlement` and reference an origin transaction.

- [ ] **Step 2: Use the predicate in the projector sort key**

Give those events priority independent of pending/confirmed status. Keep `pending_purchase_is_in_current_position` unchanged for realtime inclusion semantics.

- [ ] **Step 3: Add the historical-income regression test**

Create a WalletPlus position with confirmed opening shares, a wallet purchase whose confirmation date is later than the historical income date, and a same-day outflow larger than the confirmed opening shares but smaller than total realtime liquidity. Assert the historical confirmed position is zero rather than negative and the pending purchase does not earn income early.

- [ ] **Step 4: Track pending liquidity only in historical replay**

When `use_confirmation_date=True`, keep future-confirming WalletPlus purchases out of confirmed shares but track their shares in a local liquidity pool. Permit negative-delta wallet events to consume that pool only after confirmed shares reach zero; retain the negative-share error when the pool cannot cover the shortfall.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_portfolio_transactions.py::test_redemption_settlement_wallet_purchase_keeps_priority_when_confirmed tests/test_portfolio_income.py::test_wallet_outflow_can_consume_future_confirming_liquidity_without_negative_history -q`

Expected: `2 passed`.

- [ ] **Step 6: Run affected suites**

Run: `python -m pytest tests/test_portfolio_positions.py tests/test_portfolio_transactions.py tests/test_portfolio_sip.py tests/test_portfolio_jobs.py -q`

Expected: all tests pass.

### Task 3: Verify the complete local change

**Files:**
- Modify: none

**Interfaces:**
- Consumes: the completed code and tests
- Produces: verification evidence and a deployable commit

- [ ] **Step 1: Run all non-browser tests**

Run: `python -m pytest -q --ignore=tests/test_nav_dashboard_scroll_layout.py`

Expected: all tests pass.

- [ ] **Step 2: Compile affected Python files**

Run: `python -m py_compile src/portfolio_positions.py tests/test_portfolio_transactions.py`

Expected: exit code 0.

- [ ] **Step 3: Review the diff and commit**

Confirm only the design, plan, focused test, and projector implementation changed. Commit with `fix: keep wallet settlement order stable`.

### Task 4: Back up and deploy to production

**Files:**
- Deploy: `src/portfolio_positions.py`
- Back up: production `data/portfolio.db` and the previous `src/portfolio_positions.py`

**Interfaces:**
- Consumes: verified commit and existing production SSH access
- Produces: timestamped backup directory and deployed source

- [ ] **Step 1: Capture a read-only pre-deployment snapshot**

Record service state, git revision, the four pending transactions, wallet income dates, positions, and duplicate idempotency count.

- [ ] **Step 2: Stop the service and create consistent backups**

Stop `daily-report.service`. Use Python `sqlite3.Connection.backup` to create a timestamped database backup, and copy the previous projector file into the same backup directory.

- [ ] **Step 3: Upload and validate the source**

Upload only `src/portfolio_positions.py`, then run the production virtualenv's `py_compile` before restarting.

- [ ] **Step 4: Restart the service**

Start `daily-report.service` and verify it reaches `active` state.

### Task 5: Verify production recovery

**Files:**
- Modify: none beyond normal idempotent production settlement

**Interfaces:**
- Consumes: deployed fix and production scheduler
- Produces: confirmed backlog and an evidence-backed before/after report

- [ ] **Step 1: Wait for one portfolio cycle**

Confirm a fresh successful `Portfolio cycle` log appears and no new `negative portfolio shares` error is written.

- [ ] **Step 2: Verify approved transaction changes**

Verify the 108000 and 54015 wallet purchases are confirmed, the 18 September SIP is confirmed at NAV 1.0386 with fee 0.12, and the 21 September SIP remains pending unless its quote and confirmation date are both available.

- [ ] **Step 3: Verify derived data**

Verify wallet income for 19 and 20 September exists, all positions recalculate to stored values, and idempotency keys remain unique.

- [ ] **Step 4: Verify unaffected production behavior**

Confirm the service remains active, NAV/profit pushes are no longer gated by a failed portfolio cycle, and unrelated product transactions did not change.
