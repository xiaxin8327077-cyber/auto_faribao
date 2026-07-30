# Portfolio Ledger Phase 3: Transactions, Income, and Daily SIP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement replayable holdings, manual purchases/redemptions, cancellation and reversal, cash-management income compounding, cash dividends, and atomic trading-day SIP execution.

**Architecture:** `PositionProjector` is a deterministic reducer over immutable ledger events plus pending locks. `PortfolioTransactionService` owns all user-recorded transaction state changes. `SipService` creates one execution intent per plan/date, reserves the cash source, and confirms both sides atomically only when the exact official fund quote exists.

**Tech Stack:** Python 3.10+, `Decimal`, Phase 1 SQLite repository, Phase 2 market quotes/providers, existing scheduler loop, `pytest`.

## Global Constraints

- Prerequisites: Phase 1 and Phase 2 completion gates are green.
- Purchases are entered by amount; redemptions are entered by shares.
- Fund/NAV confirmation shares equal `amount × (1 - fee_rate) ÷ exact confirmation NAV`.
- `003103` uses 100 yuan and fee rate 0; `015736` uses 2,000 yuan and 0.006%, stored as decimal rate `0.00006`.
- Cash-management unit price is exactly 1.
- Pending redemptions lock shares; pending source transfers lock cash-management shares.
- Locked cash is excluded from available balance and from cash income effective shares starting on the real trade date.
- A linked source transfer and target purchase confirm or cancel in one SQLite transaction.
- Insufficient SIP source balance causes a skip and notification; no debt and no catch-up.
- 余额不足、暂停和非交易日均“不追补”，后续日期只执行自身当日计划。
- Pause affects future intents only; an existing pending intent continues unless explicitly cancelled.
- Resume starts with the next applicable trading date; paused dates are not backfilled.
- Cash dividends default to an external cash flow; an optional destination creates a linked cash transfer in.
- Confirmed records cannot be deleted; correction is reversal followed by a new record.
- Do not stage production database files, tokens, JSON runtime state, logs, screenshots, or unrelated changes.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/portfolio_positions.py` | Deterministic ledger replay and position persistence |
| `src/portfolio_transactions.py` | Manual trade, cancellation, reversal, adjustment, dividend services |
| `src/portfolio_income.py` | Cash-management daily income accrual |
| `src/portfolio_sip.py` | SIP plans, intents, exact-quote settlement, pause/resume |
| `src/portfolio_jobs.py` | Idempotent orchestration of quotes, pending work, accruals |
| `src/portfolio_repository.py` | Query/update methods required by services |
| `src/scheduler.py` | Periodic portfolio cycle hook |
| `tests/test_portfolio_positions.py` | Replay, locks, cost basis, rebuild |
| `tests/test_portfolio_transactions.py` | Manual flows, atomic links, cancel/reverse/adjust |
| `tests/test_portfolio_income.py` | Per-10k income and compounding |
| `tests/test_portfolio_sip.py` | Fees, trading dates, balance, pause/resume, idempotency |
| `tests/test_scheduler_cookie_recovery.py` | Existing scheduler regression plus cycle hook |

### Task 1: Implement deterministic position replay

**Files:**
- Create: `src/portfolio_positions.py`
- Create: `tests/test_portfolio_positions.py`
- Modify: `src/portfolio_repository.py`

**Interfaces:**
- Produces: `PositionProjector(repository)`.
- Produces: `calculate(product_id) -> Position`.
- Produces: `rebuild(product_id=None) -> list[Position]`.
- Repository produces: `list_transactions(product_id=None, statuses=None) -> list[Transaction]`.

- [ ] **Step 1: Write failing replay and lock tests**

```python
# tests/test_portfolio_positions.py
from datetime import date
from decimal import Decimal

from src.portfolio_models import (
    Position, Product, ProductType, Transaction, TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector


def tx(tx_id, product_id, kind, status, shares, amount="0"):
    return Transaction(
        id=tx_id,
        product_id=product_id,
        transaction_type=kind,
        status=status,
        trade_date=date(2026, 7, 30),
        idempotency_key=f"test:{tx_id}",
        shares=Decimal(shares),
        amount=Decimal(amount),
    )


def test_projector_replays_confirmed_events_and_pending_locks(repo):
    repo.add_product(Product(
        "cash", "nanyin_wealth", "NYRR000007", "日日聚宝",
        ProductType.CASH_MANAGEMENT,
    ))
    repo.create_transaction(tx(
        "open", "cash", TransactionType.OPENING_POSITION,
        TransactionStatus.CONFIRMED, "10000", "10000",
    ))
    repo.create_transaction(tx(
        "pending", "cash", TransactionType.CASH_TRANSFER_OUT,
        TransactionStatus.PENDING_QUOTE, "2000", "2000",
    ))

    position = PositionProjector(repo).calculate("cash")

    assert position.total_shares == Decimal("10000")
    assert position.locked_shares == Decimal("2000")
    assert position.available_shares == Decimal("8000")


def test_rebuild_replaces_corrupt_projection_from_ledger(repo):
    repo.add_product(Product(
        "fund", "changsheng_fund", "003103", "基金",
        ProductType.PUBLIC_FUND,
    ))
    repo.create_transaction(tx(
        "open", "fund", TransactionType.OPENING_POSITION,
        TransactionStatus.CONFIRMED, "100", "100",
    ))
    repo.replace_position(Position("fund", Decimal("9"), Decimal("0"), Decimal("9"), Decimal("9")))
    rebuilt = PositionProjector(repo).rebuild("fund")[0]
    assert rebuilt.total_shares == Decimal("100")
```

Add a shared `repo` fixture to this test file using a temporary `PortfolioDatabase`.

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_positions.py -q`

Expected: FAIL because `src.portfolio_positions` is missing.

- [ ] **Step 3: Implement the reducer**

Use this event direction table:

```python
SHARE_DIRECTION = {
    TransactionType.OPENING_POSITION: Decimal("1"),
    TransactionType.MANUAL_PURCHASE: Decimal("1"),
    TransactionType.SIP_PURCHASE: Decimal("1"),
    TransactionType.CASH_TRANSFER_IN: Decimal("1"),
    TransactionType.INCOME_ACCRUAL: Decimal("1"),
    TransactionType.HOLDING_ADJUSTMENT: Decimal("1"),
    TransactionType.MANUAL_REDEMPTION: Decimal("-1"),
    TransactionType.CASH_TRANSFER_OUT: Decimal("-1"),
    TransactionType.REVERSAL: Decimal("1"),
}
```

Reducer rules:

1. Sort by `(trade_date, created_at, id)` from the repository.
2. Confirmed events apply `shares × SHARE_DIRECTION[type]`; reversal shares are already signed.
3. Opening, purchase, transfer-in, income, and positive adjustment add their `amount` to cost basis.
4. Redemption and transfer-out reduce cost basis by the current average cost per share times removed shares.
5. Cash dividend does not change shares or cost basis.
6. `pending_quote` and `pending_confirmation` redemptions/transfer-outs add their positive shares to `locked_shares` but do not change `total_shares`.
7. Cancelled and failed rows do not affect totals. An original row whose status is `reversed` still participates in replay; its linked signed `reversal` event offsets it exactly.
8. Raise `ValueError("negative portfolio shares")` if confirmed replay falls below zero.
9. Raise `ValueError("locked shares exceed total shares")` when pending locks exceed total.

Persist rebuilt positions only after all requested products calculate successfully; use one database transaction.

- [ ] **Step 4: Run replay and repository tests**

Run: `pytest tests/test_portfolio_positions.py tests/test_portfolio_repository.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the projector**

```bash
git add src/portfolio_positions.py src/portfolio_repository.py tests/test_portfolio_positions.py
git commit -m "feat: derive portfolio positions from ledger"
```

### Task 2: Implement manual purchases and redemptions

**Files:**
- Create: `src/portfolio_transactions.py`
- Create: `tests/test_portfolio_transactions.py`
- Modify: `src/portfolio_repository.py`

**Interfaces:**
- Produces: `PortfolioTransactionService(repository, projector)`.
- Produces: `record_purchase(product_id, amount, trade_date, idempotency_key, source_cash_product_id="", fee_rate=Decimal("0"), created_by="web") -> Transaction`.
- Produces: `record_redemption(product_id, shares, trade_date, idempotency_key, destination_cash_product_id="", created_by="web") -> Transaction`.
- Produces: `confirm_pending(transaction_id, quote) -> Transaction`.
- Repository produces exact-date `get_quote(product_id, quote_date)`.

- [ ] **Step 1: Write failing manual-flow tests**

```python
# tests/test_portfolio_transactions.py
def test_fund_purchase_waits_for_exact_quote_and_locks_source(services):
    repo, transactions, projector = services
    seed_cash(repo, "cash", "3000")
    seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")

    purchase = transactions.record_purchase(
        "fund", Decimal("2000"), date(2026, 7, 30), "web:purchase-1",
        source_cash_product_id="cash", fee_rate=Decimal("0.00006"),
    )

    assert purchase.status is TransactionStatus.PENDING_QUOTE
    assert projector.calculate("cash").locked_shares == Decimal("2000")


def test_cash_purchase_with_source_confirms_both_sides_atomically(services):
    repo, transactions, projector = services
    seed_cash(repo, "source", "3000")
    seed_cash(repo, "target", "0")
    transactions.record_purchase(
        "target", Decimal("500"), date(2026, 7, 30), "web:cash-1",
        source_cash_product_id="source",
    )
    assert projector.calculate("source").total_shares == Decimal("2500")
    assert projector.calculate("target").total_shares == Decimal("500")


def test_redemption_rejects_more_than_available_shares(services):
    repo, transactions, _projector = services
    seed_cash(repo, "cash", "100")
    with pytest.raises(ValueError, match="insufficient available shares"):
        transactions.record_redemption(
            "cash", Decimal("101"), date(2026, 7, 30), "web:redeem-1"
        )
```

Provide local helpers that insert products and confirmed opening positions; do not import test helpers from another test module.

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_transactions.py -q`

Expected: FAIL because `src.portfolio_transactions` is missing.

- [ ] **Step 3: Implement manual trade creation and exact-quote confirmation**

Use these rules in `record_purchase`:

```python
if amount <= 0:
    raise ValueError("amount must be positive")
product = repo.require_product(product_id)
if product.product_type is ProductType.CASH_MANAGEMENT:
    status = TransactionStatus.CONFIRMED
    confirmation_nav = Decimal("1")
    shares = amount
else:
    quote = repo.get_quote(product_id, trade_date)
    status = (
        TransactionStatus.CONFIRMED
        if quote is not None and quote.unit_nav is not None
        else TransactionStatus.PENDING_QUOTE
    )
    confirmation_nav = quote.unit_nav if status is TransactionStatus.CONFIRMED else None
    shares = (
        amount * (Decimal("1") - fee_rate) / confirmation_nav
        if confirmation_nav is not None else None
    )
```

When a source is specified:

- Require a `cash_management` source.
- Calculate the source position inside the same `BEGIN IMMEDIATE` transaction.
- Reject when `available_shares < amount`.
- Create linked `cash_transfer_out` with `shares=amount` and the same status as the purchase.
- If pending, the projector treats transfer-out shares as locked.

`record_redemption` validates positive shares, locks them immediately for non-cash products waiting on a quote, and creates a linked `cash_transfer_in` when a destination is specified. Cash products confirm at NAV 1.

`confirm_pending` requires `quote.quote_date == transaction.trade_date`, computes fee and shares/amount, updates both linked rows in one transaction, then rebuilds the affected positions.

- [ ] **Step 4: Run transaction tests**

Run: `pytest tests/test_portfolio_transactions.py tests/test_portfolio_positions.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit manual transactions**

```bash
git add src/portfolio_transactions.py src/portfolio_repository.py tests/test_portfolio_transactions.py
git commit -m "feat: record manual portfolio transactions"
```

### Task 3: Add cancellation, reversal, and holding calibration

**Files:**
- Modify: `src/portfolio_transactions.py`
- Modify: `tests/test_portfolio_transactions.py`

**Interfaces:**
- Produces: `cancel_pending(transaction_id, idempotency_key, actor="web") -> Transaction`.
- Produces: `reverse_confirmed(transaction_id, reason, idempotency_key, actor="web") -> Transaction`.
- Produces: `adjust_holding(product_id, actual_shares, effective_date, reason, idempotency_key, actor="web") -> Transaction`.

- [ ] **Step 1: Write failing state-transition tests**

```python
def test_cancelling_pending_purchase_releases_linked_cash_lock(services):
    repo, transactions, projector = services
    seed_cash(repo, "cash", "3000")
    seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
    purchase = transactions.record_purchase(
        "fund", Decimal("2000"), date(2026, 7, 30), "web:p",
        source_cash_product_id="cash",
    )
    transactions.cancel_pending(purchase.id, "web:cancel-p")
    assert projector.calculate("cash").locked_shares == Decimal("0")


def test_confirmed_transaction_requires_reversal_not_cancel(services):
    repo, transactions, projector = services
    seed_cash(repo, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash", Decimal("100"), date(2026, 7, 30), "web:confirmed"
    )
    with pytest.raises(ValueError, match="confirmed transaction cannot be cancelled"):
        transactions.cancel_pending(purchase.id, "web:bad-cancel")
    reversal = transactions.reverse_confirmed(
        purchase.id, "录入错误", "web:reverse"
    )
    assert reversal.transaction_type is TransactionType.REVERSAL
    assert projector.calculate("cash").total_shares == Decimal("1000")


def test_holding_adjustment_records_only_the_difference(services):
    repo, transactions, projector = services
    seed_cash(repo, "cash", "1000")
    adjustment = transactions.adjust_holding(
        "cash", Decimal("998.50"), date(2026, 7, 30),
        "平台份额校准", "web:adjust",
    )
    assert adjustment.shares == Decimal("-1.50")
    assert projector.calculate("cash").total_shares == Decimal("998.50")
```

- [ ] **Step 2: Verify new tests fail**

Run: `pytest tests/test_portfolio_transactions.py -q`

Expected: FAIL because the three methods are missing.

- [ ] **Step 3: Implement guarded state transitions and audit**

Exact guards:

```python
PENDING_STATUSES = {
    TransactionStatus.PENDING_QUOTE,
    TransactionStatus.PENDING_CONFIRMATION,
}

if tx.status not in PENDING_STATUSES:
    raise ValueError("confirmed transaction cannot be cancelled")
```

Cancellation updates the target and linked row to `cancelled` in one transaction and appends one audit row.

Reversal:

1. Require original status `confirmed`.
2. Reject if an existing reversal already links the original.
3. Insert `TransactionType.REVERSAL`, status `confirmed`, with `shares=-original.shares` and `amount=-original.amount`.
4. Mark original status `reversed` and set `reversed_at`.
5. Rebuild the affected products and append audit.

Calibration computes `difference = actual_shares - current.total_shares`, inserts a confirmed `holding_adjustment`, and never modifies earlier rows. Reject a resulting negative holding.

- [ ] **Step 4: Run transaction and projector tests**

Run: `pytest tests/test_portfolio_transactions.py tests/test_portfolio_positions.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit corrections**

```bash
git add src/portfolio_transactions.py tests/test_portfolio_transactions.py
git commit -m "feat: cancel reverse and calibrate portfolio trades"
```

### Task 4: Accrue cash-management income and cash dividends

**Files:**
- Create: `src/portfolio_income.py`
- Create: `tests/test_portfolio_income.py`
- Modify: `src/portfolio_transactions.py`

**Interfaces:**
- Produces: `CashIncomeService.accrue(product_id, quote_date) -> Transaction | None`.
- Produces: `PortfolioTransactionService.record_cash_dividend(product_id, amount, dividend_date, idempotency_key, destination_cash_product_id="", actor="web") -> Transaction`.

- [ ] **Step 1: Write failing income and dividend tests**

```python
# tests/test_portfolio_income.py
def test_cash_income_uses_effective_shares_and_compounds_next_day(income_services):
    repo, projector, income = income_services
    seed_cash(repo, "cash", "10000")
    seed_quote(repo, "cash", date(2026, 7, 29), income_per_10k="0.5")
    first = income.accrue("cash", date(2026, 7, 29))
    assert first.shares == Decimal("0.5")
    seed_quote(repo, "cash", date(2026, 7, 30), income_per_10k="0.5")
    second = income.accrue("cash", date(2026, 7, 30))
    assert second.shares == Decimal("0.500025")


def test_duplicate_accrual_returns_existing_event(income_services):
    repo, _projector, income = income_services
    seed_cash(repo, "cash", "10000")
    seed_quote(repo, "cash", date(2026, 7, 29), income_per_10k="0.5")
    assert income.accrue("cash", date(2026, 7, 29)).id == \
        income.accrue("cash", date(2026, 7, 29)).id
```

Add a transaction-service test proving a dividend with no destination changes no shares, while a destination creates an equal `cash_transfer_in`.

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_income.py tests/test_portfolio_transactions.py -q`

Expected: FAIL because income accrual and dividend methods are missing.

- [ ] **Step 3: Implement per-10k accrual and dividend links**

Income calculation:

```python
effective_shares = position.total_shares - position.locked_shares
income_amount = (
    effective_shares / Decimal("10000") * quote.income_per_10k
)
```

Insert confirmed `income_accrual` with `amount=income_amount`, `shares=income_amount`, `confirmation_nav=1`, and idempotency key `income:<product_id>:<quote_date>`. A legitimate zero income produces a zero event; a missing quote or missing `income_per_10k` returns `None` and remains eligible for later retry. Negative income is allowed unless it would make total shares negative.

Dividend insertion:

- `cash_dividend` is confirmed, carries amount, and has no shares.
- With destination, require a cash-management product and insert linked confirmed `cash_transfer_in` with `shares=amount`.
- Use one database transaction and rebuild only destination holdings.

- [ ] **Step 4: Run income and transaction tests**

Run: `pytest tests/test_portfolio_income.py tests/test_portfolio_transactions.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit income and dividends**

```bash
git add src/portfolio_income.py src/portfolio_transactions.py tests/test_portfolio_income.py tests/test_portfolio_transactions.py
git commit -m "feat: accrue cash income and record dividends"
```

### Task 5: Implement SIP plans, intents, and exact-quote settlement

**Files:**
- Create: `src/portfolio_sip.py`
- Create: `tests/test_portfolio_sip.py`
- Modify: `src/portfolio_repository.py`

**Interfaces:**
- Produces: `SipService(repository, projector, transaction_service, notifier=None)`.
- Produces: `save_plan(...) -> SipPlan`, `activate(plan_id)`, `pause(plan_id)`, `resume(plan_id)`.
- Produces: `ensure_intent(plan_id, intended_date) -> PlanExecution`.
- Produces: `settle_pending(as_of_date) -> list[PlanExecution]`.
- Produces: `PlanExecution(id, plan_id, intended_trade_date, status, reason="", transaction_id="")`.
- Repository produces `save_plan`, `get_plan`, `list_plans`, `save_plan_execution`, `get_plan_execution(plan_id, intended_trade_date)`, and `list_plan_executions(plan_id)`.

- [ ] **Step 1: Write failing fee, balance, idempotency, and lifecycle tests**

```python
# tests/test_portfolio_sip.py
def test_015736_exact_quote_confirms_fee_adjusted_shares(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "3000")
    seed_fund(repo, "fund", "015736")
    plan = sip.save_plan(
        product_id="fund",
        daily_amount=Decimal("2000"),
        purchase_fee_rate=Decimal("0.00006"),
        source_cash_product_id="cash",
        start_date=date(2026, 7, 30),
    )
    sip.activate(plan.id)
    execution = sip.ensure_intent(plan.id, date(2026, 7, 30))
    assert projector.calculate("cash").locked_shares == Decimal("2000")
    seed_quote(repo, "fund", date(2026, 7, 30), unit_nav="1.0331")
    sip.settle_pending(date(2026, 7, 30))
    expected = Decimal("2000") * (Decimal("1") - Decimal("0.00006")) / Decimal("1.0331")
    assert projector.calculate("fund").total_shares == expected
    assert execution.id == sip.ensure_intent(plan.id, date(2026, 7, 30)).id


def test_insufficient_balance_skips_without_debt_or_catch_up(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1999")
    seed_fund(repo, "fund", "015736")
    plan = active_plan(sip, "fund", "cash", "2000", "0.00006")
    execution = sip.ensure_intent(plan.id, date(2026, 7, 30))
    assert execution.status == "skipped"
    assert execution.reason == "insufficient_balance"
    assert projector.calculate("cash").total_shares == Decimal("1999")


def test_pause_does_not_cancel_existing_pending_and_resume_does_not_backfill(sip_services):
    repo, sip, _projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")
    pending = sip.ensure_intent(plan.id, date(2026, 7, 30))
    sip.pause(plan.id)
    assert sip.ensure_intent(plan.id, date(2026, 7, 31)).reason == "plan_paused"
    assert sip.get_execution(pending.id).status == "pending_quote"
    sip.resume(plan.id)
    assert sip.list_executions(plan.id, date(2026, 7, 31))[-1].reason == "plan_paused"
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_sip.py -q`

Expected: FAIL because `src.portfolio_sip` is missing.

- [ ] **Step 3: Implement the state machine**

Define the immutable execution record in `src/portfolio_sip.py`:

```python
@dataclass(frozen=True)
class PlanExecution:
    id: str
    plan_id: str
    intended_trade_date: date
    status: str
    reason: str = ""
    transaction_id: str = ""
```

Plan guards:

- Only `public_fund` can be a SIP target.
- Only `cash_management` can be a source.
- `daily_amount > 0`, `0 <= purchase_fee_rate < 1`.
- Draft without a source is allowed; activation without a source raises `ValueError("cash source is required")`.

Intent rules:

1. Unique key `(plan_id, intended_trade_date)` returns the prior execution on repeat.
2. Weekend creates `skipped/non_trading_day`.
3. Paused plan creates `skipped/plan_paused`.
4. Before creating linked pending transactions, calculate current source available shares in a write transaction.
5. Insufficient balance creates `skipped/insufficient_balance`, calls `notifier`, and creates no transactions.
6. Sufficient balance creates linked `cash_transfer_out` and `sip_purchase`, status `pending_quote`; the source amount becomes locked.

Settlement rules:

1. Exact quote exists: confirm linked records atomically with fee-adjusted shares.
2. No exact quote and the repository has a later official quote: cancel linked pending rows, release lock, mark execution `skipped/non_trading_day`.
3. No exact or later quote: keep `pending_quote`.
4. Never use `latest_quote(on_or_before=intended_date)` as confirmation unless its date exactly equals `intended_date`.

- [ ] **Step 4: Run SIP tests**

Run: `pytest tests/test_portfolio_sip.py tests/test_portfolio_transactions.py tests/test_portfolio_positions.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit SIP**

```bash
git add src/portfolio_sip.py src/portfolio_repository.py tests/test_portfolio_sip.py
git commit -m "feat: add trading-day fund sip engine"
```

### Task 6: Orchestrate portfolio jobs in the scheduler

**Files:**
- Create: `src/portfolio_jobs.py`
- Create: `tests/test_portfolio_jobs.py`
- Modify: `src/scheduler.py:59-134`
- Modify: `tests/test_scheduler_cookie_recovery.py`

**Interfaces:**
- Produces: `PortfolioJobs.run_cycle(now) -> PortfolioCycleResult`.
- Produces: `PortfolioCycleResult(quotes_synced, intents_created, trades_settled, income_accrued)`.
- Scheduler helper: `_run_portfolio_cycle(cfg, now)`.
- Cycle order: sync active product quotes, create today's SIP intents, settle pending trades, accrue available cash quotes, rebuild changed positions.

- [ ] **Step 1: Write failing orchestration-order and scheduler tests**

```python
# tests/test_portfolio_jobs.py
def test_cycle_runs_network_sync_before_database_settlement():
    calls = []
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: calls.append("quotes"),
        create_intents=lambda _day: calls.append("intents"),
        settle_pending=lambda _day: calls.append("settle"),
        accrue_income=lambda _day: calls.append("income"),
    )
    jobs.run_cycle(datetime(2026, 7, 30, 18, 0))
    assert calls == ["quotes", "intents", "settle", "income"]
```

Add a scheduler test that two checks in the same 30-minute slot call `_run_portfolio_cycle` once, while the next slot calls it again.

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_jobs.py tests/test_scheduler_cookie_recovery.py -q`

Expected: FAIL because `PortfolioJobs` and the scheduler hook are missing.

- [ ] **Step 3: Implement a slot-idempotent cycle hook**

Define:

```python
@dataclass(frozen=True)
class PortfolioCycleResult:
    quotes_synced: int
    intents_created: int
    trades_settled: int
    income_accrued: int
```

Use slot key:

```python
portfolio_slot = now.strftime("%Y-%m-%dT%H:") + (
    "00" if now.minute < 30 else "30"
)
```

Store only the last attempted in-process slot in the scheduler; all business writes remain database-idempotent after restart. `_run_portfolio_cycle` constructs the Phase 1 database/repository and Phase 2 provider registry, logs a summary, and catches exceptions so the daily-report scheduler loop remains alive. It must not hold a database transaction while network providers run.

- [ ] **Step 4: Run all phase-3 focused tests**

Run: `pytest tests/test_portfolio_positions.py tests/test_portfolio_transactions.py tests/test_portfolio_income.py tests/test_portfolio_sip.py tests/test_portfolio_jobs.py tests/test_scheduler_cookie_recovery.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Run full regression and commit**

Run: `pytest -q`

Expected: all tests PASS.

```bash
git add src/portfolio_jobs.py src/scheduler.py tests/test_portfolio_jobs.py tests/test_scheduler_cookie_recovery.py
git commit -m "feat: schedule portfolio quote and ledger jobs"
```

## Phase 3 Completion Gate

- Run `git diff --check`.
- Run every Phase 1–3 focused test module.
- Run `pytest -q`.
- Rebuild positions twice and assert identical rows.
- Simulate a scheduler retry and assert no duplicate quote, accrual, SIP execution, transfer, or purchase.
- Verify a pending linked transfer is released by cancellation.
- Verify an exact 015736 quote uses fee rate `0.00006`, not `0.006`.
- Do not enable production migration or webpage write routes yet.
