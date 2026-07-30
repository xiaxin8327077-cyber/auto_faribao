from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Position,
    Product,
    ProductStatus,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
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


def test_product_round_trip_and_active_listing(repo):
    active = Product(
        "p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
        registration_code="R1", metadata_json='{"risk": "low"}',
    )
    inactive = Product(
        "p2", "cash", "000001", "现金宝", ProductType.CASH_MANAGEMENT,
        status=ProductStatus.INACTIVE,
    )
    assert repo.add_product(active) == active
    repo.add_product(inactive)

    assert repo.get_product("p1") == active
    assert repo.get_product("missing") is None
    assert repo.list_products() == [active, inactive]
    assert repo.list_products(active_only=True) == [active]


def test_duplicate_idempotency_key_returns_original_transaction(repo):
    repo.add_product(Product("p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND))
    repo.create_transaction(Transaction(
        "prior", "p1", TransactionType.MANUAL_PURCHASE,
        TransactionStatus.PENDING_QUOTE, date(2026, 7, 29), "prior-request",
    ))
    tx = Transaction(
        id="t1",
        product_id="p1",
        transaction_type=TransactionType.MANUAL_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 7, 30),
        idempotency_key="web:request-1",
        amount=Decimal("100"),
        shares=Decimal("99.5"),
        fee_amount=Decimal("0.5"),
        fee_rate=Decimal("0.005"),
        confirmation_nav=Decimal("1.005"),
        confirmation_date=date(2026, 7, 31),
        linked_transaction_id="prior",
        plan_id="plan-1",
        note="first request",
        created_by="web",
    )

    assert repo.create_transaction(tx).id == "t1"
    original = repo.create_transaction(Transaction(**{**tx.__dict__, "id": "t2"}))

    assert original == tx
    assert repo.get_transaction_by_idempotency("web:request-1") == tx
    assert repo.get_transaction_by_idempotency("missing") is None


def test_create_transaction_uses_the_supplied_transaction_connection(repo):
    repo.add_product(Product("p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND))
    tx = Transaction(
        "t1", "p1", TransactionType.MANUAL_PURCHASE,
        TransactionStatus.PENDING_QUOTE, date(2026, 7, 30), "request-1",
    )

    with repo.database.transaction() as conn:
        repo.create_transaction(tx, conn)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_replace_position_round_trips_decimals_and_missing_position_is_zero(repo):
    repo.add_product(Product("p1", "fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND))
    assert repo.get_position("p1") == Position(
        "p1", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
    )

    position = Position(
        "p1", Decimal("10.25"), Decimal("1.75"), Decimal("12"), Decimal("9.5"),
    )
    repo.replace_position(position)

    assert repo.get_position("p1") == position


def test_append_audit_persists_all_audit_fields(repo):
    repo.append_audit(
        "a1", "create", "transaction", "t1",
        before_json="{}", after_json='{"status": "pending_quote"}',
        result="success", source="web",
    )

    with repo.database.connection() as conn:
        row = conn.execute(
            """SELECT id, action, object_type, object_id, before_json, after_json,
                      result, source
               FROM audit_logs WHERE id = ?""",
            ("a1",),
        ).fetchone()

    assert tuple(row) == (
        "a1", "create", "transaction", "t1", "{}",
        '{"status": "pending_quote"}', "success", "web",
    )
