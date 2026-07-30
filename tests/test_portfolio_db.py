from decimal import Decimal
import sqlite3

import pytest

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
    optional_decimal_text,
)

from src.portfolio_db import PortfolioDatabase


def test_portfolio_enums_use_stable_database_values():
    assert ProductType.CASH_MANAGEMENT.value == "cash_management"
    assert TransactionType.SIP_PURCHASE.value == "sip_purchase"
    assert TransactionStatus.PENDING_QUOTE.value == "pending_quote"


def test_decimal_text_is_canonical_and_never_uses_float_rounding():
    assert decimal_text("2000.000") == "2000"
    assert decimal_text(Decimal("0.00006")) == "0.00006"
    assert optional_decimal_text("") is None


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


def test_connection_restores_wal_after_another_connection_changes_journal_mode(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with sqlite3.connect(db.path) as external_conn:
        assert external_conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower() == "delete"

    with db.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_database_enforces_canonical_decimal_text_for_every_accounting_field(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
        )
        conn.execute(
            """INSERT INTO quotes
               (id, product_id, quote_date, unit_nav, cumulative_nav,
                income_per_10k, seven_day_annualized_rate, source, raw_hash,
                fetched_at)
               VALUES ('q1', 'p1', '2026-01-01', '2', '2', '2', '2', 'test',
                       'hash', '2026-01-01T00:00:00')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, fee_amount, fee_rate, confirmation_nav, idempotency_key,
                created_by)
               VALUES ('t1', 'p1', 'manual_purchase', 'pending_confirmation',
                       '2026-01-01', '2', '2', '2', '2', '2', 'tx-1', 'test')"""
        )
        conn.execute(
            """INSERT INTO sip_plans
               (id, product_id, daily_amount, purchase_fee_rate, status, start_date)
               VALUES ('s1', 'p1', '2', '2', 'active', '2026-01-01')"""
        )
        conn.execute(
            """INSERT INTO positions
               (product_id, available_shares, locked_shares, total_shares, cost_basis)
               VALUES ('p1', '2', '2', '2', '2')"""
        )
        conn.execute(
            """INSERT INTO legacy_profit_history
               (id, profit_date, amount, source_kind, source_json)
               VALUES ('l1', '2026-01-01', '2', 'test', '{}')"""
        )

        for statement in (
            "UPDATE quotes SET unit_nav = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET cumulative_nav = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET income_per_10k = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET seven_day_annualized_rate = '2.00' WHERE id = 'q1'",
            "UPDATE transactions SET amount = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET shares = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET fee_amount = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET fee_rate = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET confirmation_nav = '2.00' WHERE id = 't1'",
            "UPDATE sip_plans SET daily_amount = '2.00' WHERE id = 's1'",
            "UPDATE sip_plans SET purchase_fee_rate = '2.00' WHERE id = 's1'",
            "UPDATE positions SET available_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET locked_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET total_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET cost_basis = '2.00' WHERE product_id = 'p1'",
            "UPDATE legacy_profit_history SET amount = '2.00' WHERE id = 'l1'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE quotes SET unit_nav = 'NaN' WHERE id = 'q1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE quotes SET unit_nav = 'not-a-number' WHERE id = 'q1'")


def test_confirmed_transaction_is_immutable_except_for_controlled_reversal(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, note, created_by, confirmed_at)
               VALUES ('t1', 'p1', 'manual_purchase', 'confirmed', '2026-01-01',
                       '2', '2', 'tx-1', 'original', 'test',
                       '2026-01-02T00:00:00')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE transactions SET note = 'changed' WHERE id = 't1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transactions WHERE id = 't1'")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="confirmed transaction reversal requires a valid child",
        ):
            conn.execute(
                """UPDATE transactions
                   SET status = 'reversed',
                       reversed_at = '2026-01-03T00:00:00'
                   WHERE id = 't1'"""
            )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, note,
                created_by)
               VALUES ('r1', 'p1', 'reversal', 'confirmed', '2026-01-01',
                       '-2', '-2', 't1', 'reverse-1', 'correction', 'test')"""
        )
        conn.execute(
            """UPDATE transactions
               SET status = 'reversed', reversed_at = '2026-01-03T00:00:00'
               WHERE id = 't1'"""
        )
        row = conn.execute("SELECT * FROM transactions WHERE id = 't1'").fetchone()
        assert row["status"] == "reversed"
        assert row["reversed_at"] == "2026-01-03T00:00:00"
        assert row["note"] == "original"
        assert row["amount"] == "2"
        assert row["confirmed_at"] == "2026-01-02T00:00:00"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE transactions SET note = 'changed' WHERE id = 't1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transactions WHERE id = 't1'")


def test_database_rejects_second_cash_leg_but_allows_reversal_child(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', '001', '基金', 'public_fund', 'active')"""
        )
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('cash-1', 'cash', 'cash_transfer_out', 'confirmed',
                       '2026-01-01', '100', '100', 'main', 'cash-1-key',
                       'test')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, linked_transaction_id, idempotency_key,
                    created_by)
                   VALUES ('cash-2', 'cash', 'cash_transfer_out', 'confirmed',
                           '2026-01-01', '100', '100', 'main', 'cash-2-key',
                           'test')"""
            )

        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('reversal-1', 'fund', 'reversal', 'confirmed',
                       '2026-01-01', '-100', '-50', 'main', 'reverse-1-key',
                       'test')"""
        )


def test_database_rejects_second_reversal_child_but_allows_cash_leg(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', '001', '基金', 'public_fund', 'active')"""
        )
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('cash-1', 'cash', 'cash_transfer_out', 'confirmed',
                       '2026-01-01', '100', '100', 'main', 'cash-1-key',
                       'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('reversal-1', 'fund', 'reversal', 'confirmed',
                       '2026-01-01', '-100', '-50', 'main', 'reverse-1-key',
                       'test')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, linked_transaction_id, idempotency_key,
                    created_by)
                   VALUES ('reversal-2', 'fund', 'reversal', 'confirmed',
                           '2026-01-01', '-100', '-50', 'main',
                           'reverse-2-key', 'test')"""
            )


@pytest.mark.parametrize(
    ("child_product", "child_amount", "child_shares", "child_status"),
    [
        ("other", "-100", "-50", "confirmed"),
        ("fund", "-99", "-50", "confirmed"),
        ("fund", "-100", "-49", "confirmed"),
        ("fund", "-100", "-50", "cancelled"),
    ],
)
def test_database_rejects_reversed_status_without_exact_confirmed_child(
    tmp_path,
    child_product,
    child_amount,
    child_shares,
    child_status,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        for product_id in ("fund", "other"):
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status)
                   VALUES (?, 'test', ?, ?, 'public_fund', 'active')""",
                (product_id, product_id, product_id),
            )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('child', ?, 'reversal', ?, '2026-01-01', ?, ?, 'main',
                       'child-key', 'test')""",
            (
                child_product,
                child_status,
                child_amount,
                child_shares,
            ),
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="confirmed transaction reversal requires a valid child",
        ):
            conn.execute(
                """UPDATE transactions
                   SET status = 'reversed', reversed_at = CURRENT_TIMESTAMP
                   WHERE id = 'main'"""
            )
