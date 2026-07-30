# Portfolio Ledger Phase 1: Storage and Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SQLite portfolio database, typed repository, auditable transaction primitives, and a dry-run-first migration from the existing config and dashboard JSON without switching production reads yet.

**Architecture:** Add a focused persistence package around Python's standard `sqlite3` module. Immutable business events live in `transactions`; `positions` is explicitly rebuildable. The migration creates a temporary database, validates it, backs up legacy inputs, and only then atomically installs `data/portfolio.db`.

**Tech Stack:** Python 3.10+, standard-library `sqlite3`, `dataclasses`, `enum`, `Decimal`, `pytest`.

## Global Constraints

- Database path is exactly `data/portfolio.db` in production; tests inject a temporary path.
- Enable SQLite WAL and foreign keys on every connection.
- Store accounting numbers as canonical decimal strings; convert through `Decimal(str(value))`, never `float`.
- `transactions` are immutable after confirmation; corrections use linked reversal events.
- `(provider, code)` is unique for products.
- `idempotency_key` is globally unique for business writes.
- Existing historical profit values are preserved and are not recomputed.
- This phase must not change production read paths or remove `config.yaml` product support.
- Do not stage `config.yaml`, `data/nav_dashboard_access_token`, runtime JSON, logs, screenshots, or unrelated working-tree changes.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/portfolio_models.py` | Enums, immutable records, decimal/date normalization |
| `src/portfolio_db.py` | Connections, schema versioning, transaction context |
| `src/portfolio_repository.py` | Typed product, quote, transaction, plan, position, and audit persistence |
| `src/portfolio_migration.py` | Legacy discovery, dry-run validation, backup, atomic install |
| `.gitignore` | Exclude portfolio database, WAL/SHM files, and migration backups |
| `tests/test_portfolio_db.py` | Schema, WAL, constraints, transaction rollback |
| `tests/test_portfolio_repository.py` | Repository contracts and idempotency |
| `tests/test_portfolio_migration.py` | Legacy migration, repeat safety, failure preservation |

### Task 1: Define portfolio domain records and decimal normalization

**Files:**
- Create: `src/portfolio_models.py`
- Test: `tests/test_portfolio_db.py`

**Interfaces:**
- Produces: `ProductType`, `ProductStatus`, `TransactionType`, `TransactionStatus`, `SipPlanStatus`.
- Produces: `decimal_text(value) -> str`, `optional_decimal_text(value) -> str | None`.
- Produces: immutable `Product`, `Transaction`, `Position`, and `SipPlan` dataclasses.

- [ ] **Step 1: Write failing enum and normalization tests**

```python
# tests/test_portfolio_db.py
from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
    optional_decimal_text,
)


def test_portfolio_enums_use_stable_database_values():
    assert ProductType.CASH_MANAGEMENT.value == "cash_management"
    assert TransactionType.SIP_PURCHASE.value == "sip_purchase"
    assert TransactionStatus.PENDING_QUOTE.value == "pending_quote"


def test_decimal_text_is_canonical_and_never_uses_float_rounding():
    assert decimal_text("2000.000") == "2000"
    assert decimal_text(Decimal("0.00006")) == "0.00006"
    assert optional_decimal_text("") is None
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `pytest tests/test_portfolio_db.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'src.portfolio_models'`.

- [ ] **Step 3: Create the complete domain model**

```python
# src/portfolio_models.py
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional


class ProductType(str, Enum):
    WEALTH_NAV = "wealth_nav"
    CASH_MANAGEMENT = "cash_management"
    PUBLIC_FUND = "public_fund"


class ProductStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class TransactionType(str, Enum):
    OPENING_POSITION = "opening_position"
    MANUAL_PURCHASE = "manual_purchase"
    MANUAL_REDEMPTION = "manual_redemption"
    SIP_PURCHASE = "sip_purchase"
    CASH_TRANSFER_OUT = "cash_transfer_out"
    CASH_TRANSFER_IN = "cash_transfer_in"
    INCOME_ACCRUAL = "income_accrual"
    CASH_DIVIDEND = "cash_dividend"
    REVERSAL = "reversal"
    HOLDING_ADJUSTMENT = "holding_adjustment"


class TransactionStatus(str, Enum):
    PENDING_QUOTE = "pending_quote"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REVERSED = "reversed"
    FAILED = "failed"


class SipPlanStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"


def decimal_text(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"decimal value must be finite: {value!r}")
    return format(number.normalize(), "f")


def optional_decimal_text(value) -> Optional[str]:
    if value in (None, ""):
        return None
    return decimal_text(value)


@dataclass(frozen=True)
class Product:
    id: str
    provider: str
    code: str
    name: str
    product_type: ProductType
    status: ProductStatus = ProductStatus.ACTIVE
    registration_code: str = ""
    currency: str = "CNY"
    metadata_json: str = "{}"


@dataclass(frozen=True)
class Transaction:
    id: str
    product_id: str
    transaction_type: TransactionType
    status: TransactionStatus
    trade_date: date
    idempotency_key: str
    amount: Optional[Decimal] = None
    shares: Optional[Decimal] = None
    fee_amount: Optional[Decimal] = None
    fee_rate: Optional[Decimal] = None
    confirmation_nav: Optional[Decimal] = None
    confirmation_date: Optional[date] = None
    linked_transaction_id: str = ""
    plan_id: str = ""
    note: str = ""
    created_by: str = "system"


@dataclass(frozen=True)
class Position:
    product_id: str
    available_shares: Decimal
    locked_shares: Decimal
    total_shares: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class SipPlan:
    id: str
    product_id: str
    daily_amount: Decimal
    purchase_fee_rate: Decimal
    source_cash_product_id: str
    status: SipPlanStatus
    start_date: date
```

- [ ] **Step 4: Run the focused tests**

Run: `pytest tests/test_portfolio_db.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit the model**

```bash
git add src/portfolio_models.py tests/test_portfolio_db.py
git commit -m "feat: add portfolio domain models"
```

### Task 2: Create the SQLite schema and transaction boundary

**Files:**
- Create: `src/portfolio_db.py`
- Modify: `.gitignore`
- Modify: `tests/test_portfolio_db.py`

**Interfaces:**
- Consumes: enums are stored through their `.value`.
- Produces: `PortfolioDatabase(path)`.
- Produces: `PortfolioDatabase.initialize() -> None`.
- Produces: `PortfolioDatabase.connection() -> sqlite3.Connection`.
- Produces: `PortfolioDatabase.transaction() -> ContextManager[sqlite3.Connection]`.

- [ ] **Step 1: Add failing schema and rollback tests**

```python
# tests/test_portfolio_db.py
import sqlite3

import pytest

from src.portfolio_db import PortfolioDatabase


def test_database_initializes_versioned_schema_with_wal_and_foreign_keys(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "products", "quotes", "transactions", "sip_plans",
            "plan_executions", "positions", "audit_logs",
            "legacy_profit_history", "schema_migrations",
        } <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_transaction_rolls_back_every_write_on_error(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status)
                   VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
            )
            raise RuntimeError("stop")

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
```

- [ ] **Step 2: Verify the tests fail before implementation**

Run: `pytest tests/test_portfolio_db.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.portfolio_db'`.

- [ ] **Step 3: Implement the database and complete schema**

Create `src/portfolio_db.py` with:

```python
from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "portfolio.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    code TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    product_type TEXT NOT NULL CHECK (
        product_type IN ('wealth_nav', 'cash_management', 'public_fund')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    registration_code TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, code)
);
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    quote_date TEXT NOT NULL,
    unit_nav TEXT,
    cumulative_nav TEXT,
    income_per_10k TEXT,
    seven_day_annualized_rate TEXT,
    source TEXT NOT NULL,
    source_timestamp TEXT,
    raw_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, quote_date)
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    confirmation_date TEXT,
    amount TEXT,
    shares TEXT,
    fee_amount TEXT,
    fee_rate TEXT,
    confirmation_nav TEXT,
    linked_transaction_id TEXT REFERENCES transactions(id),
    plan_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    reversed_at TEXT
);
CREATE TABLE IF NOT EXISTS sip_plans (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    daily_amount TEXT NOT NULL,
    purchase_fee_rate TEXT NOT NULL,
    source_cash_product_id TEXT REFERENCES products(id),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused')),
    start_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paused_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_executions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES sip_plans(id),
    intended_trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    transaction_id TEXT REFERENCES transactions(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id, intended_trade_date)
);
CREATE TABLE IF NOT EXISTS positions (
    product_id TEXT PRIMARY KEY REFERENCES products(id),
    available_shares TEXT NOT NULL,
    locked_shares TEXT NOT NULL,
    total_shares TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS legacy_profit_history (
    id TEXT PRIMARY KEY,
    profit_date TEXT NOT NULL,
    amount TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_json TEXT NOT NULL,
    UNIQUE(profit_date, source_kind, source_json)
);
CREATE INDEX IF NOT EXISTS idx_quotes_product_date
    ON quotes(product_id, quote_date DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_product_date
    ON transactions(product_id, trade_date, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_status
    ON transactions(status, trade_date);
"""


class PortfolioDatabase:
    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def connection(self):
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
```

Add these runtime exclusions to `.gitignore`:

```gitignore
data/*.db
data/*.db-wal
data/*.db-shm
data/portfolio-backups/
```

- [ ] **Step 4: Run database tests**

Run: `pytest tests/test_portfolio_db.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the schema**

```bash
git add .gitignore src/portfolio_db.py tests/test_portfolio_db.py
git commit -m "feat: add portfolio sqlite schema"
```

### Task 3: Implement typed repository and idempotent writes

**Files:**
- Create: `src/portfolio_repository.py`
- Create: `tests/test_portfolio_repository.py`

**Interfaces:**
- Consumes: `PortfolioDatabase`, `Product`, `Transaction`, `SipPlan`.
- Produces: `PortfolioRepository`.
- Produces: `add_product(product) -> Product`, `get_product(product_id) -> Product | None`, `list_products(active_only=False) -> list[Product]`.
- Produces: `create_transaction(transaction, conn=None) -> Transaction`; duplicate idempotency returns the original row.
- Produces: `replace_position(position, conn=None) -> None`, `get_position(product_id) -> Position`.
- Produces: `append_audit(...) -> None`.

- [ ] **Step 1: Write failing repository tests**

```python
# tests/test_portfolio_repository.py
from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Product, ProductType, Transaction, TransactionStatus, TransactionType,
)
from src.portfolio_repository import PortfolioRepository


@pytest.fixture
def repo(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    return PortfolioRepository(db)


def test_product_provider_and_code_are_unique(repo):
    product = Product("p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND)
    repo.add_product(product)
    with pytest.raises(ValueError, match="product already exists"):
        repo.add_product(Product("p2", "fund", "003103", "重复", ProductType.PUBLIC_FUND))


def test_duplicate_idempotency_key_returns_original_transaction(repo):
    repo.add_product(Product("p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND))
    tx = Transaction(
        id="t1",
        product_id="p1",
        transaction_type=TransactionType.MANUAL_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 7, 30),
        idempotency_key="web:request-1",
        amount=Decimal("100"),
    )
    assert repo.create_transaction(tx).id == "t1"
    assert repo.create_transaction(Transaction(**{**tx.__dict__, "id": "t2"})).id == "t1"
```

- [ ] **Step 2: Verify tests fail before repository creation**

Run: `pytest tests/test_portfolio_repository.py -q`

Expected: FAIL with missing `src.portfolio_repository`.

- [ ] **Step 3: Implement explicit row mappers and repository methods**

Create `src/portfolio_repository.py`. Use explicit column lists; never use `SELECT *` in row mappers. The transaction insertion must follow this exact idempotency sequence:

```python
class PortfolioRepository:
    def __init__(self, database):
        self.database = database

    def create_transaction(self, tx, conn=None):
        if conn is None:
            with self.database.transaction() as owned:
                return self.create_transaction(tx, owned)
        existing = conn.execute(
            """SELECT id, product_id, transaction_type, status, trade_date,
                      confirmation_date, amount, shares, fee_amount, fee_rate,
                      confirmation_nav, linked_transaction_id, plan_id,
                      idempotency_key, note, created_by
               FROM transactions WHERE idempotency_key = ?""",
            (tx.idempotency_key,),
        ).fetchone()
        if existing:
            return self._transaction_from_row(existing)
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                confirmation_date, amount, shares, fee_amount, fee_rate,
                confirmation_nav, linked_transaction_id, plan_id,
                idempotency_key, note, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx.id, tx.product_id, tx.transaction_type.value, tx.status.value,
                tx.trade_date.isoformat(),
                tx.confirmation_date.isoformat() if tx.confirmation_date else None,
                optional_decimal_text(tx.amount), optional_decimal_text(tx.shares),
                optional_decimal_text(tx.fee_amount), optional_decimal_text(tx.fee_rate),
                optional_decimal_text(tx.confirmation_nav),
                tx.linked_transaction_id or None, tx.plan_id or None,
                tx.idempotency_key, tx.note, tx.created_by,
            ),
        )
        return tx
```

Implement `add_product`, `get_product`, `list_products`, `get_transaction_by_idempotency`, `replace_position`, `get_position`, and `append_audit` with the field names defined in Task 1. Convert every numeric text back through `Decimal`.

- [ ] **Step 4: Run repository and database tests**

Run: `pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the repository**

```bash
git add src/portfolio_repository.py tests/test_portfolio_repository.py
git commit -m "feat: add portfolio repository"
```

### Task 4: Build dry-run-first legacy migration

**Files:**
- Create: `src/portfolio_migration.py`
- Create: `tests/test_portfolio_migration.py`

**Interfaces:**
- Consumes: `Config`, legacy `data/nav_dashboard_state.json`, `PortfolioDatabase`, `PortfolioRepository`.
- Produces: `MigrationReport(products, opening_positions, profit_rows, installed, backup_dir)`.
- Produces: `migrate_legacy_portfolio(cfg, state_path, target_path, backup_root, install=False) -> MigrationReport`.
- Product type override: `NYRR000007` and `AM264381F` migrate as `cash_management`; all other legacy NAV products migrate as `wealth_nav`.

- [ ] **Step 1: Write migration success, repeat, and failure tests**

```python
# tests/test_portfolio_migration.py
import json

import pytest

from src.config import Config
from src.portfolio_db import PortfolioDatabase
from src.portfolio_migration import migrate_legacy_portfolio


def legacy_cfg():
    return Config({"nav_monitor": {"products": [
        {"provider": "nanyin_wealth", "code": "NYRR000007", "name": "日日聚宝", "shares": 10936.10},
        {"provider": "citic_wealth", "code": "AF233276B", "name": "净值理财", "shares": 2000},
    ]}})


def test_migration_dry_run_does_not_install_database(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "profit_entries": [{"nav_date": "2026-07-29", "profit": "12.34"}],
        "manual_daily_profits": {"2026-07-28": "9.87"},
    }), encoding="utf-8")
    target = tmp_path / "portfolio.db"

    report = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=False
    )

    assert report.products == 2
    assert report.opening_positions == 2
    assert report.profit_rows == 2
    assert report.installed is False
    assert not target.exists()


def test_install_is_repeat_safe_and_preserves_original_inputs(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    first = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    second = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    assert first.installed is True
    assert second.installed is False
    assert state.exists()
    with PortfolioDatabase(target).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_failed_validation_never_replaces_target(tmp_path, monkeypatch):
    target = tmp_path / "portfolio.db"
    target.write_bytes(b"existing")
    monkeypatch.setattr(
        "src.portfolio_migration._validate_candidate",
        lambda *_args: (_ for _ in ()).throw(ValueError("mismatch")),
    )
    with pytest.raises(ValueError, match="mismatch"):
        migrate_legacy_portfolio(
            legacy_cfg(), tmp_path / "missing.json", target,
            tmp_path / "backups", install=True,
        )
    assert target.read_bytes() == b"existing"
```

- [ ] **Step 2: Verify tests fail before migration implementation**

Run: `pytest tests/test_portfolio_migration.py -q`

Expected: FAIL with missing `src.portfolio_migration`.

- [ ] **Step 3: Implement candidate creation, validation, backup, and atomic install**

Implement:

```python
@dataclass(frozen=True)
class MigrationReport:
    products: int
    opening_positions: int
    profit_rows: int
    installed: bool
    backup_dir: str


LEGACY_PRODUCT_TYPE_OVERRIDES = {
    ("nanyin_wealth", "NYRR000007"): ProductType.CASH_MANAGEMENT,
    ("citic_wealth", "AM264381F"): ProductType.CASH_MANAGEMENT,
}
```

The migration algorithm must execute exactly in this order:

1. If `target_path` already contains schema version 1, validate and return `installed=False`.
2. Create the candidate beside the target as `portfolio.db.migrating-<uuid>`.
3. Insert products with deterministic IDs `uuid5(NAMESPACE_URL, f"{provider}:{code.upper()}")`.
4. Insert one confirmed `opening_position` per non-empty legacy share value using idempotency key `migration:opening:<product-id>`.
5. Copy each old automatic and manual profit value to `legacy_profit_history`; preserve the source JSON verbatim.
6. `_validate_candidate` checks product count, opening-position count, and exact `Decimal` share totals per product.
7. When `install=False`, close and delete only the candidate and return the counts.
8. When `install=True`, copy existing legacy inputs into `backup_root/YYYYmmdd-HHMMSS/`, fsync the copies, then use `os.replace(candidate, target_path)`.
9. On any exception, close and delete only the candidate; never delete or overwrite the existing target or legacy inputs.

- [ ] **Step 4: Run all phase-1 tests**

Run: `pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_migration.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Run full regression and commit**

Run: `pytest -q`

Expected: all existing and new tests PASS.

```bash
git add src/portfolio_migration.py tests/test_portfolio_migration.py
git commit -m "feat: add portfolio legacy migration"
```

## Phase 1 Completion Gate

- Run `git diff --check`.
- Run `pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_migration.py -q`.
- Run `pytest -q`.
- Confirm no production `data/portfolio.db` was created by tests.
- Confirm `git status --short` contains no staged runtime data or unrelated user files.
- Do not activate SQLite reads yet; Phase 4 performs the production cutover after all consumers exist.
