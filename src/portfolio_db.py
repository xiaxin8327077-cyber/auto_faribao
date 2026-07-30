from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3


BASE_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "portfolio.db"


def _is_canonical_decimal(value) -> int:
    if value is None:
        return 1
    if not isinstance(value, str):
        return 0
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if not decimal_value.is_finite():
        return 0
    return int(format(decimal_value.normalize(), "f") == value)


def _is_decimal_negation(left, right) -> int:
    if left is None or right is None:
        return 0
    try:
        left_value = Decimal(left)
        right_value = Decimal(right)
    except (InvalidOperation, ValueError, TypeError):
        return 0
    return int(
        left_value.is_finite()
        and right_value.is_finite()
        and left_value == -right_value
    )


BASE_SCHEMA_SQL = """
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
CREATE TRIGGER IF NOT EXISTS validate_quote_decimals_on_insert
BEFORE INSERT ON quotes
WHEN NOT (
    canonical_decimal(NEW.unit_nav)
    AND canonical_decimal(NEW.cumulative_nav)
    AND canonical_decimal(NEW.income_per_10k)
    AND canonical_decimal(NEW.seven_day_annualized_rate)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_quote_decimals_on_update
BEFORE UPDATE ON quotes
WHEN NOT (
    canonical_decimal(NEW.unit_nav)
    AND canonical_decimal(NEW.cumulative_nav)
    AND canonical_decimal(NEW.income_per_10k)
    AND canonical_decimal(NEW.seven_day_annualized_rate)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_transaction_decimals_on_insert
BEFORE INSERT ON transactions
WHEN NOT (
    canonical_decimal(NEW.amount)
    AND canonical_decimal(NEW.shares)
    AND canonical_decimal(NEW.fee_amount)
    AND canonical_decimal(NEW.fee_rate)
    AND canonical_decimal(NEW.confirmation_nav)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_transaction_decimals_on_update
BEFORE UPDATE ON transactions
WHEN NOT (
    canonical_decimal(NEW.amount)
    AND canonical_decimal(NEW.shares)
    AND canonical_decimal(NEW.fee_amount)
    AND canonical_decimal(NEW.fee_rate)
    AND canonical_decimal(NEW.confirmation_nav)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_sip_plan_decimals_on_insert
BEFORE INSERT ON sip_plans
WHEN NOT (
    canonical_decimal(NEW.daily_amount)
    AND canonical_decimal(NEW.purchase_fee_rate)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_sip_plan_decimals_on_update
BEFORE UPDATE ON sip_plans
WHEN NOT (
    canonical_decimal(NEW.daily_amount)
    AND canonical_decimal(NEW.purchase_fee_rate)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_position_decimals_on_insert
BEFORE INSERT ON positions
WHEN NOT (
    canonical_decimal(NEW.available_shares)
    AND canonical_decimal(NEW.locked_shares)
    AND canonical_decimal(NEW.total_shares)
    AND canonical_decimal(NEW.cost_basis)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_position_decimals_on_update
BEFORE UPDATE ON positions
WHEN NOT (
    canonical_decimal(NEW.available_shares)
    AND canonical_decimal(NEW.locked_shares)
    AND canonical_decimal(NEW.total_shares)
    AND canonical_decimal(NEW.cost_basis)
)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_legacy_profit_decimal_on_insert
BEFORE INSERT ON legacy_profit_history
WHEN NOT canonical_decimal(NEW.amount)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS validate_legacy_profit_decimal_on_update
BEFORE UPDATE ON legacy_profit_history
WHEN NOT canonical_decimal(NEW.amount)
BEGIN
    SELECT RAISE(ABORT, 'accounting decimals must be canonical finite text');
END;
CREATE TRIGGER IF NOT EXISTS prevent_confirmed_transaction_mutation
BEFORE UPDATE ON transactions
WHEN OLD.status = 'confirmed' AND NOT (
    NEW.status = 'reversed'
    AND OLD.reversed_at IS NULL
    AND NEW.reversed_at IS NOT NULL
    AND NEW.id IS OLD.id
    AND NEW.product_id IS OLD.product_id
    AND NEW.transaction_type IS OLD.transaction_type
    AND NEW.trade_date IS OLD.trade_date
    AND NEW.confirmation_date IS OLD.confirmation_date
    AND NEW.amount IS OLD.amount
    AND NEW.shares IS OLD.shares
    AND NEW.fee_amount IS OLD.fee_amount
    AND NEW.fee_rate IS OLD.fee_rate
    AND NEW.confirmation_nav IS OLD.confirmation_nav
    AND NEW.linked_transaction_id IS OLD.linked_transaction_id
    AND NEW.plan_id IS OLD.plan_id
    AND NEW.idempotency_key IS OLD.idempotency_key
    AND NEW.note IS OLD.note
    AND NEW.created_by IS OLD.created_by
    AND NEW.created_at IS OLD.created_at
    AND NEW.confirmed_at IS OLD.confirmed_at
)
BEGIN
    SELECT RAISE(ABORT, 'confirmed transaction is immutable except for reversal');
END;
CREATE TRIGGER IF NOT EXISTS prevent_reversed_transaction_mutation
BEFORE UPDATE ON transactions
WHEN OLD.status = 'reversed'
BEGIN
    SELECT RAISE(ABORT, 'reversed transaction is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_final_transaction_delete
BEFORE DELETE ON transactions
WHEN OLD.status IN ('confirmed', 'reversed')
BEGIN
    SELECT RAISE(ABORT, 'confirmed and reversed transactions cannot be deleted');
END;
"""


V2_SCHEMA_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_cash_leg_per_transaction
    ON transactions(linked_transaction_id)
    WHERE linked_transaction_id IS NOT NULL
      AND transaction_type IN ('cash_transfer_out', 'cash_transfer_in');
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_reversal_child_per_transaction
    ON transactions(linked_transaction_id)
    WHERE linked_transaction_id IS NOT NULL
      AND transaction_type = 'reversal';
CREATE TRIGGER IF NOT EXISTS prevent_unbacked_confirmed_reversal
BEFORE UPDATE ON transactions
WHEN OLD.status = 'confirmed'
    AND NEW.status = 'reversed'
    AND NOT EXISTS (
        SELECT 1
        FROM transactions AS child
        WHERE child.transaction_type = 'reversal'
          AND child.status = 'confirmed'
          AND child.linked_transaction_id = OLD.id
          AND child.product_id = OLD.product_id
          AND decimal_negation(child.shares, OLD.shares)
          AND decimal_negation(child.amount, OLD.amount)
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'confirmed transaction reversal requires a valid child'
    );
END;
"""


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete schema statement")


class PortfolioDatabase:
    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_function(
            "canonical_decimal", 1, _is_canonical_decimal, deterministic=True
        )
        conn.create_function(
            "decimal_negation", 2, _is_decimal_negation, deterministic=True
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            if not self._has_schema_migrations(conn):
                self._create_current_schema(conn)
                return
            versions = {
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            if versions == {SCHEMA_VERSION}:
                return
            if versions != {BASE_SCHEMA_VERSION}:
                raise ValueError(
                    "unsupported portfolio schema version set: "
                    f"{sorted(versions)}"
                )
            self._preflight_v2(conn)
            self._upgrade_v1_to_v2(conn)

    @staticmethod
    def _has_schema_migrations(conn) -> bool:
        return conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'schema_migrations'"""
        ).fetchone() is not None

    @staticmethod
    def _create_current_schema(conn) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _execute_sql_script(conn, BASE_SCHEMA_SQL)
            _execute_sql_script(conn, V2_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _upgrade_v1_to_v2(self, conn) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._preflight_v2(conn)
            _execute_sql_script(conn, V2_SCHEMA_SQL)
            conn.execute("DELETE FROM schema_migrations")
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    @staticmethod
    def _preflight_v2(conn) -> None:
        duplicate_cash = conn.execute(
            """SELECT linked_transaction_id
               FROM transactions
               WHERE linked_transaction_id IS NOT NULL
                 AND transaction_type IN (
                     'cash_transfer_out', 'cash_transfer_in'
                 )
               GROUP BY linked_transaction_id
               HAVING COUNT(*) > 1
               LIMIT 1"""
        ).fetchone()
        if duplicate_cash is not None:
            raise ValueError(
                "portfolio v2 migration blocked: duplicate cash links"
            )
        duplicate_reversal = conn.execute(
            """SELECT linked_transaction_id
               FROM transactions
               WHERE linked_transaction_id IS NOT NULL
                 AND transaction_type = 'reversal'
               GROUP BY linked_transaction_id
               HAVING COUNT(*) > 1
               LIMIT 1"""
        ).fetchone()
        if duplicate_reversal is not None:
            raise ValueError(
                "portfolio v2 migration blocked: "
                "duplicate reversal children"
            )
        invalid_reversal = conn.execute(
            """SELECT child.id
               FROM transactions AS child
               LEFT JOIN transactions AS parent
                 ON parent.id = child.linked_transaction_id
               WHERE child.transaction_type = 'reversal'
                 AND child.status IN ('confirmed', 'reversed')
                 AND (
                     parent.id IS NULL
                     OR child.product_id != parent.product_id
                     OR NOT decimal_negation(
                         child.shares, parent.shares
                     )
                     OR NOT decimal_negation(
                         child.amount, parent.amount
                     )
                 )
               LIMIT 1"""
        ).fetchone()
        if invalid_reversal is not None:
            raise ValueError(
                "portfolio v2 migration blocked: invalid reversal data"
            )
        unbacked_reversed = conn.execute(
            """SELECT parent.id
               FROM transactions AS parent
               WHERE parent.status = 'reversed'
                 AND NOT EXISTS (
                     SELECT 1
                     FROM transactions AS child
                     WHERE child.transaction_type = 'reversal'
                       AND child.status IN ('confirmed', 'reversed')
                       AND child.linked_transaction_id = parent.id
                       AND child.product_id = parent.product_id
                       AND decimal_negation(
                           child.shares, parent.shares
                       )
                       AND decimal_negation(
                           child.amount, parent.amount
                       )
                 )
               LIMIT 1"""
        ).fetchone()
        if unbacked_reversed is not None:
            raise ValueError(
                "portfolio v2 migration blocked: "
                "unbacked reversed transactions"
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
