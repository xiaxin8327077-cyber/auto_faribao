from decimal import Decimal

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
