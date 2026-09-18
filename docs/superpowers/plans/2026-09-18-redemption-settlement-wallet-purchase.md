# 赎回到账与钱包 Plus 申购状态 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让赎回资金在到账前不进入钱包 Plus，到账时生成遵循现有 T+1 规则的钱包 Plus 申购，并按最近状态变化时间排列交易记录。

**Architecture:** 保留交易确认 `status`，为赎回增加独立到账状态和来源/去向字段；每日任务在赎回确认后单独推进到账，再创建幂等的钱包 Plus `manual_purchase`。数据库版本升至 7，生产历史数据通过专用可预演修正命令追加反向事件，不直接覆盖已确认金额。

**Tech Stack:** Python 3.12、SQLite、pytest、Flask API payload、原生 HTML/CSS/JavaScript、PowerShell 7、systemd。

## Global Constraints

- 系统仍以用户填写的 `settlement_date` 为到账依据，不连接真实理财平台。
- 钱包 Plus 自动申购使用到账日 00:00 北京时间作为提交时点，并完整复用现有交易日与 T+1 确认规则。
- 赎回确认只减少源产品份额；钱包 Plus 只在对应申购确认后增加。
- 已确认金额、份额、净值和确认日期保持不可变；历史修正必须追加反向事件并写审计日志。
- 线上修正只允许处理规格中列出的两笔赎回 ID，任何金额、日期或产品不匹配都必须停止。
- 所有数据库升级、到账任务和修正命令必须幂等且事务化。
- 交易记录按 `status_updated_at`、`id` 从新到旧稳定排序。

---

## File Map

- `src/portfolio_db.py`：版本 7 DDL、升级链、索引和不可变触发器。
- `src/portfolio_models.py`：到账状态枚举和交易生命周期字段。
- `src/portfolio_repository.py`：新字段持久化、状态时间更新、来源申购查询。
- `src/portfolio_transactions.py`：赎回确认、到账推进、钱包 Plus 自动申购和冲正联动。
- `src/portfolio_positions.py`：确认赎回继续减少源持仓，未确认自动申购不进入钱包持仓。
- `src/portfolio_jobs.py`：确认 → 到账 → 再确认的固定任务顺序。
- `src/portfolio_view.py`：显示状态、自动申购来源和最近状态排序。
- `src/nav_dashboard_page.html`：待到账/已到账文案及交易详情。
- `src/portfolio_repairs.py`：已知两笔线上异常的只读预演与幂等修正。
- `scripts/repair_redemption_wallet_settlement.py`：生产修正命令入口。
- `tests/test_portfolio_db.py`、`tests/test_portfolio_repository.py`：版本 7 与仓储测试。
- `tests/test_portfolio_transactions.py`、`tests/test_portfolio_jobs.py`：状态机和调度测试。
- `tests/test_portfolio_view.py`、`tests/test_nav_dashboard_page.py`：排序与页面测试。
- `tests/test_portfolio_repairs.py`：生产副本修正行为测试。

---

### Task 1: 数据库版本 7 与交易生命周期字段

**Files:**
- Modify: `src/portfolio_db.py:9-10,83-104,263-428,470-630`
- Modify: `src/portfolio_models.py:35-42,112-133`
- Modify: `src/portfolio_repository.py:27-32,150-300,782-819`
- Test: `tests/test_portfolio_db.py`
- Test: `tests/test_portfolio_repository.py`

**Interfaces:**
- Produces: `RedemptionSettlementStatus`, expanded `Transaction`, schema version 7, `PortfolioRepository.find_purchase_by_origin()` and auxiliary-state updates.
- Consumes: existing SQLite transaction table and version 1→6 upgrade chain.

- [ ] **Step 1: Write failing schema and repository tests**

Add tests that require the version 7 columns, exact v6 backfill, round-trip serialization, and one purchase per source redemption:

```python
def test_v6_upgrade_adds_redemption_settlement_columns(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    with sqlite3.connect(db.path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            CREATE TABLE products (id TEXT PRIMARY KEY);
            CREATE TABLE transactions (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                status TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                confirmation_date TEXT,
                amount TEXT,
                shares TEXT,
                fee_amount TEXT,
                fee_rate TEXT,
                confirmation_nav TEXT,
                linked_transaction_id TEXT,
                plan_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                note TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TEXT,
                reversed_at TEXT,
                trade_time TEXT NOT NULL DEFAULT '',
                settlement_date TEXT
            );
            INSERT INTO schema_migrations(version) VALUES (6);
            INSERT INTO products(id) VALUES ('wealth'), ('wallet-plus');
            """
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                idempotency_key, created_by, created_at, confirmed_at)
               VALUES ('r1', 'wealth', 'manual_redemption', 'confirmed',
                       '2026-09-17', 'r1', 'web',
                       '2026-09-16 15:13:59', '2026-09-17 16:00:28')"""
        )
        conn.execute(
            "UPDATE transactions SET settlement_date = '2026-09-18' WHERE id = 'r1'"
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                confirmation_date, amount, shares, confirmation_nav,
                linked_transaction_id, idempotency_key, created_by, created_at,
                confirmed_at, settlement_date)
               VALUES
               ('r2', 'wealth', 'manual_redemption', 'confirmed',
                '2026-09-17', '2026-09-18', '54015', '50000', '1.0803',
                'leg2', 'r2', 'web', '2026-09-16 15:13:59',
                '2026-09-17 16:00:28', '2026-09-20'),
               ('leg2', 'wallet-plus', 'cash_transfer_in', 'confirmed',
                '2026-09-17', '2026-09-18', '54015', '54015', '1',
                'r2', 'leg2', 'web', '2026-09-16 15:13:59',
                '2026-09-17 16:00:28', NULL)"""
        )
        PortfolioDatabase._upgrade_v6_to_v7(conn)

    with db.connection() as conn:
        columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(transactions)"
        )}
        rows = {row["id"]: row for row in conn.execute(
            """SELECT id, destination_cash_product_id,
                      settlement_status, status_updated_at
               FROM transactions WHERE id IN ('r1', 'r2')"""
        )}
        version = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()[0]
    assert {
        "destination_cash_product_id", "origin_transaction_id",
        "settlement_status", "settled_at", "status_updated_at",
    } <= columns
    assert rows["r1"]["status_updated_at"] == "2026-09-17 16:00:28"
    assert rows["r1"]["settlement_status"] == "pending"
    assert rows["r2"]["destination_cash_product_id"] == "wallet-plus"
    assert rows["r2"]["settlement_status"] == "pending"
    assert version == 7


def test_transaction_round_trips_redemption_lifecycle_fields(repository):
    tx = Transaction(
        id="r1",
        product_id="wealth",
        transaction_type=TransactionType.MANUAL_REDEMPTION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 9, 17),
        idempotency_key="r1",
        amount=Decimal("54015"),
        shares=Decimal("50000"),
        destination_cash_product_id="wallet-plus",
        settlement_date=date(2026, 9, 20),
        settlement_status=RedemptionSettlementStatus.PENDING,
    )
    stored = repository.create_transaction(tx)
    assert stored.destination_cash_product_id == "wallet-plus"
    assert stored.settlement_status is RedemptionSettlementStatus.PENDING
    assert stored.created_at
    assert stored.status_updated_at
```

Also add trigger tests for the two permitted confirmed-redemption lifecycle
transitions (`NULL -> pending` for legacy conversion and `pending -> settled`
for normal settlement). Assert that changing amount, shares, NAV, dates,
destination after it is set, or origin on a confirmed transaction still raises
`sqlite3.IntegrityError`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest -q tests/test_portfolio_db.py::test_v6_upgrade_adds_redemption_settlement_columns tests/test_portfolio_repository.py::test_transaction_round_trips_redemption_lifecycle_fields -v
```

Expected: FAIL because schema version 7, the new columns, enum, and transaction fields do not exist.

- [ ] **Step 3: Implement version 7, model fields, and repository persistence**

Add the enum and fields:

```python
class RedemptionSettlementStatus(str, Enum):
    PENDING = "pending"
    SETTLED = "settled"


@dataclass(frozen=True)
class Transaction:
    # existing fields remain unchanged
    destination_cash_product_id: str = ""
    origin_transaction_id: str = ""
    settlement_status: Optional[RedemptionSettlementStatus] = None
    settled_at: Optional[str] = None
    created_at: Optional[str] = None
    status_updated_at: Optional[str] = None
```

Raise `SCHEMA_VERSION` and add the upgrade:

```python
SCHEMA_VERSION = 7

@staticmethod
def _upgrade_v6_to_v7(conn) -> None:
    conn.execute("DROP TRIGGER IF EXISTS prevent_confirmed_transaction_mutation")
    conn.execute("DROP TRIGGER IF EXISTS prevent_reversed_transaction_mutation")
    columns = {row["name"] for row in conn.execute(
        "PRAGMA table_info(transactions)"
    )}
    additions = {
        "destination_cash_product_id":
            "TEXT REFERENCES products(id)",
        "origin_transaction_id":
            "TEXT REFERENCES transactions(id)",
        "settlement_status": (
            "TEXT CHECK (settlement_status IS NULL OR "
            "settlement_status IN ('pending', 'settled'))"
        ),
        "settled_at": "TEXT",
        "status_updated_at": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}")
    conn.execute(
        """UPDATE transactions
           SET status_updated_at = COALESCE(reversed_at, confirmed_at, created_at)
           WHERE status_updated_at IS NULL"""
    )
    conn.execute(
        """UPDATE transactions AS redemption
           SET destination_cash_product_id = (
               SELECT linked.product_id
               FROM transactions AS linked
               WHERE linked.id = redemption.linked_transaction_id
                 AND linked.transaction_type = 'cash_transfer_in'
           )
           WHERE redemption.transaction_type = 'manual_redemption'
             AND redemption.destination_cash_product_id IS NULL
             AND redemption.linked_transaction_id IS NOT NULL"""
    )
    conn.execute(
        """UPDATE transactions
           SET settlement_status = 'pending'
           WHERE transaction_type = 'manual_redemption'
             AND status = 'confirmed'
             AND settlement_date IS NOT NULL
             AND settlement_status IS NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_one_purchase_per_redemption
           ON transactions(origin_transaction_id)
           WHERE origin_transaction_id IS NOT NULL
             AND transaction_type = 'manual_purchase'"""
    )
    conn.execute("DELETE FROM schema_migrations")
    conn.execute("INSERT INTO schema_migrations(version) VALUES (7)")
```

Extend `_TRANSACTION_COLUMNS`, create/update SQL, `_transaction_from_row()`, and add:

```python
def find_purchase_by_origin(self, origin_transaction_id, conn=None):
    query = (
        f"SELECT {_TRANSACTION_COLUMNS} FROM transactions "
        "WHERE origin_transaction_id = ? "
        "AND transaction_type = 'manual_purchase' ORDER BY id"
    )
    if conn is None:
        with self.database.connection() as owned:
            rows = owned.execute(query, (origin_transaction_id,)).fetchall()
    else:
        rows = conn.execute(query, (origin_transaction_id,)).fetchall()
    if len(rows) > 1:
        raise ValueError("duplicate redemption purchase")
    return self._transaction_from_row(rows[0]) if rows else None

def update_redemption_settlement(
    self, transaction_id, settlement_status, *, settled_at=False, conn=None
):
    if conn is None:
        with self.database.transaction() as owned:
            return self.update_redemption_settlement(
                transaction_id,
                settlement_status,
                settled_at=settled_at,
                conn=owned,
            )
    status_value = (
        settlement_status.value
        if isinstance(settlement_status, RedemptionSettlementStatus)
        else settlement_status
    )
    cursor = conn.execute(
        """UPDATE transactions
           SET settlement_status = ?,
               settled_at = CASE
                   WHEN ? THEN COALESCE(settled_at, CURRENT_TIMESTAMP)
                   ELSE settled_at
               END,
               status_updated_at = CASE
                   WHEN COALESCE(settlement_status, '') != ?
                       THEN CURRENT_TIMESTAMP
                   ELSE status_updated_at
               END
           WHERE id = ?
             AND transaction_type = 'manual_redemption'
             AND status = 'confirmed'""",
        (status_value, int(settled_at), status_value, transaction_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("confirmed redemption not found")
    return self.get_transaction_by_id(transaction_id, conn=conn)
```

After all backfills, recreate `prevent_confirmed_transaction_mutation` and
`prevent_reversed_transaction_mutation` in the v7 DDL. Preserve immutability of
every existing financial and identity field, including the new origin and
destination fields. Permit only these auxiliary changes on a confirmed
`manual_redemption`:

- normal settlement: `settlement_status` changes from `pending` to `settled`,
  `settled_at` changes from `NULL` to a timestamp, and `status_updated_at`
  advances;
- legacy conversion: `settlement_status` may change from `NULL` to `pending`
  (or remain `pending` after migration), the destination may be set only when
  previously empty (or remain unchanged), and the obsolete linked cash leg may
  change only from its existing ID to empty after that leg is `reversed`;
- normal reversal: status changes from `confirmed` to `reversed`,
  `reversed_at` is set, and `status_updated_at` advances, while all lifecycle
  linkage fields remain unchanged.

Update every repository status transition (pending confirmation, cancellation,
reversal, and settlement) so `status_updated_at` changes in the same SQL
statement as the state. Repository inserts must write
`COALESCE(?, CURRENT_TIMESTAMP)` explicitly, so upgraded databases do not rely
on an ALTER TABLE default. V6 rows backfill from `reversed_at`, then
`confirmed_at`, then `created_at`.

For fresh databases, define `status_updated_at TEXT NOT NULL DEFAULT
CURRENT_TIMESTAMP` in `BASE_SCHEMA_SQL`. SQLite cannot add that non-constant
default with `ALTER TABLE`, so upgraded databases add the nullable column,
backfill it in the migration transaction, and install validation triggers that
reject future NULL inserts or updates. Tests must cover both fresh and upgraded
databases.

The upgrade runs inside the existing `BEGIN IMMEDIATE` transaction. Dropping
the old triggers before backfill is necessary because those triggers reject any
update to confirmed rows; recreate the stricter v7 triggers before writing
schema version 7 so a failure rolls back columns, data, triggers, and version as
one unit. Update `_create_current_schema()`, `initialize()`, and
`upgrade_v1_validated()` so every fresh or historical path finishes with the
same v7 columns and triggers.

- [ ] **Step 4: Run database and repository tests GREEN**

Run:

```powershell
pytest -q tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_migration.py -v
```

Expected: PASS, including every historical upgrade step and rollback test.

- [ ] **Step 5: Commit**

```powershell
git add src/portfolio_db.py src/portfolio_models.py src/portfolio_repository.py tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_migration.py
git commit -m "feat: 增加赎回到账生命周期字段"
```

---

### Task 2: 赎回确认、到账和钱包 Plus 自动申购

**Files:**
- Modify: `src/portfolio_transactions.py:40-342,470-609,611-900,1428-1497,1913-2415`
- Modify: `src/portfolio_positions.py:25-150`
- Test: `tests/test_portfolio_transactions.py`
- Test: `tests/test_portfolio_positions.py`

**Interfaces:**
- Consumes: Task 1 transaction fields and repository methods.
- Produces: `PortfolioTransactionService.settle_redemptions(as_of_date) -> list[Transaction]` returning newly settled source redemptions.

- [ ] **Step 1: Write failing confirmation and settlement tests**

Add one end-to-end service test for the exact desired lifecycle:

```python
def test_redemption_waits_for_settlement_then_creates_t1_wallet_purchase(services):
    repository, transactions, projector = services
    wealth = seed_product(repository, "wealth", ProductType.WEALTH_NAV, "W001")
    seed_opening_position(repository, wealth.id, "100000", "100000")
    repository.add_product(Product(
        id="wallet-plus",
        provider="wallet_plus",
        code="WALLETPLUS",
        name="钱包Plus",
        product_type=ProductType.CASH_MANAGEMENT,
    ))
    seed_opening_position(repository, "wallet-plus", "1000", "1000")
    quote = seed_quote(repository, wealth, date(2026, 9, 17), Decimal("1.0803"))

    redemption = transactions.record_redemption(
        wealth.id,
        Decimal("50000"),
        date(2026, 9, 17),
        "web:redemption-settlement",
        destination_cash_product_id="wallet-plus",
        trade_time="2026-09-16T23:13:00",
        settlement_date=date(2026, 9, 20),
    )
    confirmed = transactions.confirm_pending(
        redemption.id, quote, as_of_date=date(2026, 9, 18)
    )

    assert confirmed.status is TransactionStatus.CONFIRMED
    assert confirmed.settlement_status is RedemptionSettlementStatus.PENDING
    assert projector.calculate(wealth.id).total_shares == Decimal("50000")
    assert projector.calculate("wallet-plus").total_shares == Decimal("1000")
    assert transactions.settle_redemptions(date(2026, 9, 19)) == []

    settled = transactions.settle_redemptions(date(2026, 9, 20))
    purchase = repository.find_purchase_by_origin(redemption.id)
    assert settled[0].settlement_status is RedemptionSettlementStatus.SETTLED
    assert purchase.transaction_type is TransactionType.MANUAL_PURCHASE
    assert purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert purchase.amount == Decimal("54015")
    assert purchase.trade_date == date(2026, 9, 21)
    assert projector.calculate("wallet-plus").total_shares == Decimal("1000")

    transactions.settle_pending(date(2026, 9, 22))
    assert projector.calculate("wallet-plus").total_shares == Decimal("55015")
```

Add separate tests for external destinations, repeat settlement, transaction rollback, pending-purchase cancellation during source reversal, and confirmed-purchase group reversal.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run:

```powershell
pytest -q tests/test_portfolio_transactions.py -k "redemption_waits_for_settlement or redemption_settlement_is_idempotent or settlement_reversal" -v
```

Expected: FAIL because confirmation still creates and confirms `cash_transfer_in`, and `settle_redemptions()` does not exist.

- [ ] **Step 3: Stop creating destination cash legs during redemption submission**

Change `record_redemption()` so it validates the destination product, stores its ID on the source redemption, and creates only the source transaction:

```python
redemption = Transaction(
    id=str(uuid4()),
    product_id=product.id,
    transaction_type=TransactionType.MANUAL_REDEMPTION,
    status=status,
    trade_date=trade_date,
    idempotency_key=idempotency_key,
    trade_time=trade_time,
    amount=amount,
    shares=shares,
    confirmation_nav=nav,
    confirmation_date=(trade_date if status is TransactionStatus.CONFIRMED else None),
    destination_cash_product_id=destination.id if destination else "",
    settlement_date=settlement_date,
    settlement_status=(
        RedemptionSettlementStatus.PENDING
        if status is TransactionStatus.CONFIRMED and settlement_date
        else None
    ),
    note=note,
    created_by=created_by,
)
redemption = self.repository.create_transaction(redemption, conn)
```

Update idempotency matching to compare `destination_cash_product_id` directly rather than requiring a linked cash leg.

- [ ] **Step 4: Confirm redemptions without crediting the destination**

In `confirm_pending()`, set confirmed redemption fields and `settlement_status=PENDING`, update only the source transaction, and rebuild only the source position. Keep ordinary purchase and SIP linked-leg behavior unchanged.

```python
confirmed = replace(
    transaction,
    status=TransactionStatus.CONFIRMED,
    amount=amount,
    confirmation_nav=nav,
    confirmation_date=confirmed_on,
    settlement_status=(
        RedemptionSettlementStatus.PENDING
        if transaction.settlement_date is not None
        else None
    ),
)
```

- [ ] **Step 5: Implement idempotent settlement and automatic purchase**

Add:

```python
def settle_redemptions(self, as_of_date) -> list[Transaction]:
    settled = []
    for redemption in self.repository.list_transactions():
        if (
            redemption.transaction_type is not TransactionType.MANUAL_REDEMPTION
            or redemption.status is not TransactionStatus.CONFIRMED
            or redemption.settlement_status
               is not RedemptionSettlementStatus.PENDING
            or redemption.settlement_date is None
            or redemption.settlement_date > as_of_date
        ):
            continue
        if redemption.linked_transaction_id:
            logger.warning(
                "skip legacy linked redemption until repair: %s",
                redemption.id,
            )
            continue
        settled.append(self._settle_redemption(redemption.id, as_of_date))
    return settled
```

`_settle_redemption()` must open one database transaction and re-read the
source record. For an external-wallet redemption (empty
`destination_cash_product_id`), mark only the source as settled. For a cash
product destination, set the source to settled and create exactly one
`manual_purchase` with:

```python
submitted_at = datetime.combine(redemption.settlement_date, time.min)
schedule = confirmation_schedule(
    destination,
    TransactionType.MANUAL_PURCHASE,
    submitted_at,
)
purchase = Transaction(
    id=str(uuid4()),
    product_id=destination.id,
    transaction_type=TransactionType.MANUAL_PURCHASE,
    status=TransactionStatus.PENDING_CONFIRMATION,
    trade_date=schedule.trade_date,
    trade_time=submitted_at.isoformat(timespec="seconds"),
    idempotency_key=f"redemption-settlement:{redemption.id}",
    amount=redemption.amount,
    shares=redemption.amount,
    fee_amount=ZERO,
    fee_rate=ZERO,
    confirmation_nav=ONE,
    origin_transaction_id=redemption.id,
    created_by="redemption_settlement",
)
```

Do not set `confirmation_date` until the normal T+1 confirmer runs. Repeated
settlement returns the already settled source without inserting another
purchase. If a source has a destination but no settlement date, leave it
unsettled and surface it as a validation error rather than crediting the
destination.

The legacy-link guard is required for safe deployment: schema v7 derives the
destination and pending settlement state from old `cash_transfer_in` pairs, but
must never create a second wallet credit while the old confirmed leg is still
attached. The production repair reverses that leg and clears the link before
calling the normal settlement path. Add a regression test that a due legacy
linked redemption is skipped with no new purchase.

- [ ] **Step 6: Implement reversal rules for the new relationship**

Before reversing a settled redemption, query `find_purchase_by_origin()`:

- pending purchase: cancel it in the same transaction, then append only the source redemption reversal;
- confirmed purchase: append reversal events for both source redemption and wallet purchase;
- missing purchase for a wallet destination: reject as inconsistent;
- reversed purchase: accept only when the source reversal chain matches.

Update `_mark_reversed()` to set `status_updated_at = CURRENT_TIMESTAMP` and keep the existing immutable financial fields unchanged.

- [ ] **Step 7: Run transaction and projection tests GREEN**

Run:

```powershell
pytest -q tests/test_portfolio_transactions.py tests/test_portfolio_positions.py tests/test_portfolio_income.py -v
```

Expected: PASS. Existing purchase, redemption, SIP, cancellation, reversal, income, and corruption-detection tests remain green after updating obsolete expectations about immediate cash transfer.

- [ ] **Step 8: Commit**

```powershell
git add src/portfolio_transactions.py src/portfolio_positions.py tests/test_portfolio_transactions.py tests/test_portfolio_positions.py tests/test_portfolio_income.py
git commit -m "fix: 赎回到账后再发起钱包申购"
```

---

### Task 3: 每日任务按确认、到账、再次确认的顺序运行

**Files:**
- Modify: `src/portfolio_jobs.py:22-64,79-205`
- Test: `tests/test_portfolio_jobs.py`
- Test: `tests/test_portfolio_end_to_end.py`

**Interfaces:**
- Consumes: `settle_pending(day)` and `settle_redemptions(day)` from Task 2.
- Produces: deterministic cycle order and a `redemptions_settled` result count.

- [ ] **Step 1: Write failing cycle-order test**

```python
def test_cycle_confirms_then_settles_redemptions_then_confirms_again():
    calls = []
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: calls.append("quotes"),
        create_intents=lambda _day: calls.append("intents"),
        settle_pending=lambda _day: calls.append("confirm"),
        settle_redemptions=lambda _day: calls.append("redemption-settlement"),
        accrue_income=lambda _day: calls.append("income"),
    )

    jobs.run_cycle(datetime(2026, 9, 18, 9, 0))

    assert calls == [
        "quotes", "intents", "confirm", "redemption-settlement",
        "confirm", "income",
    ]
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
pytest -q tests/test_portfolio_jobs.py::test_cycle_confirms_then_settles_redemptions_then_confirms_again -v
```

Expected: FAIL because `PortfolioJobs` has no `settle_redemptions` callback.

- [ ] **Step 3: Implement cycle ordering and counts**

Expand the result and cycle:

```python
@dataclass(frozen=True)
class PortfolioCycleResult:
    quotes_synced: int
    intents_created: int
    trades_settled: int
    redemptions_settled: int
    income_accrued: int


first_confirmed = int(self.settle_pending(day) or 0)
redemptions = int(self.settle_redemptions(day) or 0)
second_confirmed = int(self.settle_pending(day) or 0)
settled = first_confirmed + second_confirmed
```

In `build_portfolio_jobs()`, wire `lambda day: len(transaction_service.settle_redemptions(day))`. Update zero-result and logging call sites to the five-field result.

- [ ] **Step 4: Run job and end-to-end tests GREEN**

Run:

```powershell
pytest -q tests/test_portfolio_jobs.py tests/test_portfolio_end_to_end.py -v
```

Expected: PASS, including repeated-slot idempotency and the weekend wallet purchase lifecycle.

- [ ] **Step 5: Commit**

```powershell
git add src/portfolio_jobs.py tests/test_portfolio_jobs.py tests/test_portfolio_end_to_end.py
git commit -m "feat: 自动推进赎回到账与钱包申购"
```

---

### Task 4: 交易状态展示与最近状态时间排序

**Files:**
- Modify: `src/portfolio_view.py:33-83,405-504`
- Modify: `src/nav_dashboard_page.html:1556-1688`
- Test: `tests/test_portfolio_view.py`
- Test: `tests/test_nav_dashboard_page.py`

**Interfaces:**
- Consumes: Task 1 lifecycle timestamps and Task 2 source purchase relationship.
- Produces: payload fields `display_status`, `settlement_status`, `settled_at`, `status_updated_at`, `purchase_origin`; newest-state-first transaction order.

- [ ] **Step 1: Write failing payload-order and status tests**

```python
def test_transactions_sort_by_latest_status_change_and_show_settlement_state(
    portfolio_fixture,
):
    repo = portfolio_fixture
    for transaction_id, status_updated_at in (
        ("older-created-newer-state", "2026-09-18 01:00:00"),
        ("newer-created-older-state", "2026-09-17 23:00:00"),
    ):
        repo.create_transaction(Transaction(
            id=transaction_id,
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 1),
            idempotency_key=transaction_id,
            amount=Decimal("10"),
            shares=Decimal("10"),
            status_updated_at=status_updated_at,
        ))

    payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))

    assert [row["id"] for row in payload["transactions"][:2]] == [
        "older-created-newer-state", "newer-created-older-state",
    ]


def test_pending_and_settled_redemptions_have_distinct_display_statuses(
    portfolio_fixture,
):
    payload = build_portfolio_payload(portfolio_fixture)
    by_id = {row["id"]: row for row in payload["transactions"]}
    assert by_id["pending-redemption"]["display_status"] == "pending_settlement"
    assert by_id["settled-redemption"]["display_status"] == "settled"
    assert by_id["wallet-purchase"]["purchase_origin"].endswith("赎回到账")
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
pytest -q tests/test_portfolio_view.py -k "latest_status_change or distinct_display_statuses" -v
```

Expected: FAIL because the payload sorts by `trade_time` and omits lifecycle fields.

- [ ] **Step 3: Implement backend sorting and payload fields**

Replace the ordering key with:

```python
ordered_transactions = sorted(
    transactions,
    key=lambda transaction: (
        transaction.status_updated_at or transaction.created_at or "",
        transaction.id,
    ),
    reverse=True,
)
```

Set display state in `_transaction_row()`:

```python
display_status = transaction.status.value
if (
    transaction.transaction_type is TransactionType.MANUAL_REDEMPTION
    and transaction.status is TransactionStatus.CONFIRMED
):
    if transaction.settlement_status is RedemptionSettlementStatus.PENDING:
        display_status = "pending_settlement"
    elif transaction.settlement_status is RedemptionSettlementStatus.SETTLED:
        display_status = "settled"
```

Resolve `origin_transaction_id` to a source product name and emit `purchase_origin = "<名称>赎回到账"`.

- [ ] **Step 4: Write failing page-contract tests**

```python
def test_page_labels_redemption_settlement_and_preserves_backend_order(page):
    assert "pending_settlement: '待到账'" in page
    assert "settled: '已到账'" in page
    assert "rowValue(row, 'display_status', 'status')" in page
    render_transactions = page[
        page.index("function renderTransactions()"):
        page.index("function renderSip()")
    ]
    assert ".sort(" not in render_transactions
    assert "赎回到账" in page
```

- [ ] **Step 5: Run and verify RED**

Run:

```powershell
pytest -q tests/test_nav_dashboard_page.py -k "redemption_settlement" -v
```

Expected: FAIL because the page knows only pending/confirmed/cancelled/reversed.

- [ ] **Step 6: Implement page labels and details**

Update mapping and lookup:

```javascript
function transactionStatusLabel(value) {
  return ({
    pending: '待确认',
    pending_settlement: '待到账',
    settled: '已到账',
    confirmed: '已确认',
    cancelled: '已取消',
    reversed: '已冲正',
  })[String(value || '').toLowerCase()] || esc(value || '--');
}
```

Use `display_status` only for the label and filter bucket, keep the real
`status` for cancellation/action eligibility, preserve the payload order, and
show `purchase_origin` as the wallet Plus申购资金来源. Map
`pending_settlement` to the pending filter bucket and `settled` to the confirmed
filter bucket without changing their visible labels.

- [ ] **Step 7: Run view and page tests GREEN**

Run:

```powershell
pytest -q tests/test_portfolio_view.py tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py -v
```

Expected: PASS with no frontend contract regression.

- [ ] **Step 8: Commit**

```powershell
git add src/portfolio_view.py src/nav_dashboard_page.html tests/test_portfolio_view.py tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py
git commit -m "feat: 展示赎回到账状态并按状态时间排序"
```

---

### Task 5: 两笔生产异常的可预演幂等修正

**Files:**
- Create: `src/portfolio_repairs.py`
- Create: `scripts/repair_redemption_wallet_settlement.py`
- Create: `tests/test_portfolio_repairs.py`
- Modify: `src/portfolio_transactions.py` only if a private reversal helper is shared.

**Interfaces:**
- Consumes: Task 1 schema/repository and Task 2 settlement service.
- Produces: `preview_known_redemption_repairs(repository, as_of_date) -> list[dict]` and `repair_known_redemptions(repository, as_of_date) -> list[dict]`.

- [ ] **Step 1: Write failing repair preview and execution tests**

```python
@pytest.fixture
def known_anomaly_database(tmp_path):
    database = PortfolioDatabase(tmp_path / "known-anomaly.db")
    database.initialize()
    repo = PortfolioRepository(database)
    repo.add_product(Product(
        "721c838d-b670-58e4-9c02-f1fc95a8e80a",
        "test",
        "known-wealth",
        "慧盈象固收增强一年持有期5号B",
        ProductType.WEALTH_NAV,
    ))
    repo.add_product(Product(
        "wallet-plus", "test", "wallet-plus", "钱包Plus",
        ProductType.CASH_MANAGEMENT,
    ))
    for product_id, amount in (
        ("721c838d-b670-58e4-9c02-f1fc95a8e80a", "150000"),
        ("wallet-plus", "1000"),
    ):
        repo.create_transaction(Transaction(
            id=f"opening:{product_id}",
            product_id=product_id,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 1),
            idempotency_key=f"opening:{product_id}",
            amount=Decimal(amount),
            shares=Decimal(amount),
        ))
    for source_id, leg_id, trade_date, trade_time, confirmed_on, amount, shares, nav, settled_on in (
        ("658e58e8-a483-4bd3-bed5-0d614f8e534b", "482b5541-24f8-45ce-aaab-273246980f16", date(2026, 9, 16), "2026-09-16T03:09:00", date(2026, 9, 17), "108000", "100000", "1.08", date(2026, 9, 18)),
        ("eb4fc341-d594-43ef-a0ae-1bc930ea66f8", "46a53177-7de1-4341-859a-77c1b3efa6ee", date(2026, 9, 17), "2026-09-16T23:13:00", date(2026, 9, 18), "54015", "50000", "1.0803", date(2026, 9, 20)),
    ):
        source = repo.create_transaction(Transaction(
            id=source_id,
            product_id="721c838d-b670-58e4-9c02-f1fc95a8e80a",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=trade_date,
            trade_time=trade_time,
            confirmation_date=confirmed_on,
            idempotency_key=f"legacy:{source_id}",
            amount=Decimal(amount),
            shares=Decimal(shares),
            confirmation_nav=Decimal(nav),
            settlement_date=settled_on,
            created_by="web",
        ))
        repo.create_transaction(Transaction(
            id=leg_id,
            product_id="wallet-plus",
            transaction_type=TransactionType.CASH_TRANSFER_IN,
            status=TransactionStatus.CONFIRMED,
            trade_date=trade_date,
            trade_time=trade_time,
            confirmation_date=confirmed_on,
            idempotency_key=f"linked:{source_id}",
            amount=Decimal(amount),
            shares=Decimal(amount),
            confirmation_nav=Decimal("1"),
            linked_transaction_id=source_id,
            created_by="web",
        ))
        repo.update_pending_transaction(replace(
            source,
            status=TransactionStatus.CONFIRMED,
            linked_transaction_id=leg_id,
        ))
    PositionProjector(repo).rebuild()
    return database


def test_preview_is_read_only_and_reports_exact_known_repairs(
    known_anomaly_database,
):
    repo = PortfolioRepository(known_anomaly_database)
    before = known_anomaly_database.path.read_bytes()
    preview = preview_known_redemption_repairs(repo, date(2026, 9, 18))
    assert [(row["redemption_id"], row["amount"]) for row in preview] == [
        ("658e58e8-a483-4bd3-bed5-0d614f8e534b", "108000"),
        ("eb4fc341-d594-43ef-a0ae-1bc930ea66f8", "54015"),
    ]
    assert known_anomaly_database.path.read_bytes() == before


def test_repair_reverses_premature_wallet_credit_and_is_idempotent(
    known_anomaly_database,
):
    repo = PortfolioRepository(known_anomaly_database)
    projector = PositionProjector(repo)
    wallet_before = projector.calculate("wallet-plus").total_shares
    first = repair_known_redemptions(repo, date(2026, 9, 18))
    second = repair_known_redemptions(repo, date(2026, 9, 18))
    wallet = projector.calculate("wallet-plus")
    assert all(row["result"] == "repaired" for row in first)
    assert all(row["result"] == "already_repaired" for row in second)
    assert wallet.total_shares == wallet_before - Decimal("162015")
    assert repo.find_purchase_by_origin(
        "658e58e8-a483-4bd3-bed5-0d614f8e534b"
    ).status is TransactionStatus.PENDING_CONFIRMATION
    assert repo.find_purchase_by_origin(
        "eb4fc341-d594-43ef-a0ae-1bc930ea66f8"
    ) is None
```

Also test that one mismatched expected amount aborts the whole batch without writes.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
pytest -q tests/test_portfolio_repairs.py -v
```

Expected: FAIL because repair module and CLI do not exist.

- [ ] **Step 3: Implement exact expectations and atomic repair**

Use immutable expectations:

```python
KNOWN_REDEMPTIONS = {
    "658e58e8-a483-4bd3-bed5-0d614f8e534b": {
        "product_id": "721c838d-b670-58e4-9c02-f1fc95a8e80a",
        "legacy_cash_transaction_id": "482b5541-24f8-45ce-aaab-273246980f16",
        "trade_date": date(2026, 9, 16),
        "trade_time": "2026-09-16T03:09:00",
        "shares": Decimal("100000"),
        "amount": Decimal("108000"),
        "confirmation_nav": Decimal("1.08"),
        "confirmation_date": date(2026, 9, 17),
        "settlement_date": date(2026, 9, 18),
    },
    "eb4fc341-d594-43ef-a0ae-1bc930ea66f8": {
        "product_id": "721c838d-b670-58e4-9c02-f1fc95a8e80a",
        "legacy_cash_transaction_id": "46a53177-7de1-4341-859a-77c1b3efa6ee",
        "trade_date": date(2026, 9, 17),
        "trade_time": "2026-09-16T23:13:00",
        "shares": Decimal("50000"),
        "amount": Decimal("54015"),
        "confirmation_nav": Decimal("1.0803"),
        "confirmation_date": date(2026, 9, 18),
        "settlement_date": date(2026, 9, 20),
    },
}
```

Within one `BEGIN IMMEDIATE` transaction, validate every source and legacy cash leg before any write. For each unrepaired row:

1. create an equal negative `reversal` child for the legacy `cash_transfer_in`;
2. mark only that legacy wallet leg `reversed` with `reversed_at` and `status_updated_at`;
3. verify the migrated wallet product ID, or set it only if still empty;
4. preserve/set source `settlement_status=pending` and clear its obsolete linked destination dependency;
5. append an audit row with actor `ops:redemption-settlement-repair`;
6. rebuild the source and wallet positions.

Commit only after all rows validate and all writes succeed. Then call `settle_redemptions(as_of_date)` so due sources create normal wallet purchases.

- [ ] **Step 4: Implement a dry-run-by-default CLI**

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    database = PortfolioDatabase(Path(args.database))
    repository = PortfolioRepository(database)
    if args.execute:
        database.initialize()
        result = repair_known_redemptions(repository, args.as_of)
    else:
        result = preview_known_redemption_repairs(repository, args.as_of)
    print(json.dumps(result, ensure_ascii=False, indent=2))
```

`preview_known_redemption_repairs()` must open the database with a SQLite URI
using `mode=ro`, verify schema version 7, and pass that one read-only connection
to every repository query. It must not call `initialize()`, switch journal mode,
append audit rows, or rebuild positions. No token, configuration secret, or
unrelated portfolio record may be printed.

- [ ] **Step 5: Run repair tests GREEN**

Run:

```powershell
pytest -q tests/test_portfolio_repairs.py tests/test_portfolio_transactions.py -v
```

Expected: PASS. Preview leaves the database byte-for-byte unchanged; execute is idempotent.

- [ ] **Step 6: Commit**

```powershell
git add src/portfolio_repairs.py scripts/repair_redemption_wallet_settlement.py tests/test_portfolio_repairs.py src/portfolio_transactions.py
git commit -m "fix: 安全修正两笔提前计入钱包的赎回"
```

---

### Task 6: 完整验证、Seoul 部署和生产数据修正

**Files:**
- Verify: all modified source and test files.
- Deploy: only runtime files from Tasks 1-5 plus `scripts/repair_redemption_wallet_settlement.py`.
- Never deploy: `config.yaml`, `.env`, `data/`, tests, docs, tokens, keys.

**Interfaces:**
- Consumes: all previous tasks and the `auto-faribao-seoul-prod-deploy` skill.
- Produces: verified production service, database backup, code backup, repair audit output, and post-repair read-only evidence.

- [ ] **Step 1: Run targeted tests**

```powershell
pytest -q tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_transactions.py tests/test_portfolio_positions.py tests/test_portfolio_jobs.py tests/test_portfolio_view.py tests/test_nav_dashboard_page.py tests/test_portfolio_repairs.py -v
```

Expected: PASS with no warning or error output.

- [ ] **Step 2: Run full verification**

```powershell
pytest -q
python -m compileall -q src scripts
git diff --check
git status --short
```

Expected: all tests pass, compilation and diff checks exit 0, and status contains only intended files before the final commit.

- [ ] **Step 3: Commit integration-only changes**

```powershell
git add src tests scripts
git commit -m "test: 验证赎回到账与钱包申购完整流程"
```

When no integration-only changes remain, record that the earlier task commits already contain the complete implementation and continue without creating an empty commit.

- [ ] **Step 4: Read deployment baseline and select minimal runtime files**

```powershell
ssh -i "$env:USERPROFILE\.ssh\shouer_aliyun.pem" -p 2222 root@8.213.145.226 "cat /home/ubuntu/daily_report/.codex-deploy-state.json"
git diff --name-only --diff-filter=ACMR 76740b6071be483794ed583b6a49cf7423e603b3..HEAD
```

Expected: candidate list includes only the runtime files needed by this fix and excludes configuration/data.

- [ ] **Step 5: Run deployment dry-run**

```powershell
& "$env:USERPROFILE\.codex\skills\auto-faribao-seoul-prod-deploy\scripts\deploy.ps1" `
  -Files src/portfolio_db.py,src/portfolio_models.py,src/portfolio_repository.py,src/portfolio_transactions.py,src/portfolio_positions.py,src/portfolio_jobs.py,src/portfolio_view.py,src/nav_dashboard_page.html,src/portfolio_repairs.py,scripts/repair_redemption_wallet_settlement.py `
  -Commit HEAD
```

Expected: local tests pass and dry-run prints a new code backup directory without uploading.

- [ ] **Step 6: Create a separate production database backup**

Use a new explicit path under `/home/ubuntu/daily_report/backups/`, verify it
does not already exist, record the live file owner/mode, and use Python's SQLite
online-backup API. Do not `cp` only the main file while WAL mode is active.

```powershell
$backupCommand = @'
test ! -e /home/ubuntu/daily_report/backups/20260918_redemption_settlement_db &&
mkdir /home/ubuntu/daily_report/backups/20260918_redemption_settlement_db &&
stat -c '%U:%G:%a' /home/ubuntu/daily_report/data/portfolio.db > /home/ubuntu/daily_report/backups/20260918_redemption_settlement_db/live-file-mode.txt &&
python3 -c "import sqlite3; src=sqlite3.connect('file:/home/ubuntu/daily_report/data/portfolio.db?mode=ro', uri=True); dst=sqlite3.connect('/home/ubuntu/daily_report/backups/20260918_redemption_settlement_db/portfolio.db'); src.backup(dst); assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; dst.close(); src.close()"
'@
ssh -i "$env:USERPROFILE\.ssh\shouer_aliyun.pem" -p 2222 root@8.213.145.226 $backupCommand
```

Expected: command exits 0, `PRAGMA quick_check` is `ok`, and schema version plus
transaction count from the backup match a read-only snapshot of the live
database. A byte hash need not match because SQLite online backup may compact
pages.

- [ ] **Step 7: Deploy runtime files**

Run the exact same deployment command as Step 5 with `-Execute`.

Expected: upload, server compilation, service restart, HTTP checks, authenticated read-only API check, file hashes, and deploy-state recording all pass; automatic rollback is not triggered.

- [ ] **Step 8: Preview the production repair**

```powershell
ssh -i "$env:USERPROFILE\.ssh\shouer_aliyun.pem" -p 2222 root@8.213.145.226 "cd /home/ubuntu/daily_report && python3 scripts/repair_redemption_wallet_settlement.py --database data/portfolio.db --as-of 2026-09-18"
```

Expected: exactly two known redemption IDs, amounts 108000 and 54015, no unrelated transactions, and no database write.

- [ ] **Step 9: Execute the production repair**

Run the same command with `--execute`.

Expected: two legacy wallet credits are reversed; the 108000 source is settled and has one pending wallet purchase; the 54015 source remains pending settlement until 2026-09-20.

- [ ] **Step 10: Verify service and data read-only**

Verify:

```powershell
ssh -i "$env:USERPROFILE\.ssh\shouer_aliyun.pem" -p 2222 root@8.213.145.226 "systemctl is-active daily-report"
```

Then use a read-only SQLite connection to assert:

- service is `active`;
- schema version is 7;
- both premature `cash_transfer_in` rows are reversed with matching reversal children;
- wallet Plus no longer includes 162015 yuan of prematurely confirmed transfers;
- the 108000 wallet purchase is pending confirmation and points to its source redemption;
- the 54015 redemption is pending settlement and has no purchase yet;
- payload transaction order is descending by `status_updated_at`;
- `/`, `/nav`, `/nav-assets/refresh-cw.svg`, and authenticated `/api/nav-dashboard` are healthy.

- [ ] **Step 11: Record rollback evidence**

Report the code deployment backup directory, database backup path, deployed commit SHA, runtime file list, service state, test counts, repair result, and access URL. If repair validation fails, stop the service before replacing the live SQLite database, preserve the failed database for forensics, restore the online backup and recorded owner/mode together with the code backup, restart, and rerun read-only health checks before reporting recovery.
