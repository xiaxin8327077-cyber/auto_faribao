from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Position,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository


@pytest.fixture
def repo(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    return PortfolioRepository(database)


def product(product_id):
    return Product(
        product_id,
        "test_provider",
        product_id,
        f"Product {product_id}",
        ProductType.PUBLIC_FUND,
    )


def tx(
    tx_id,
    product_id,
    kind,
    status,
    shares,
    amount="0",
    trade_date=date(2026, 7, 30),
    linked_transaction_id="",
):
    return Transaction(
        id=tx_id,
        product_id=product_id,
        transaction_type=kind,
        status=status,
        trade_date=trade_date,
        idempotency_key=f"test:{tx_id}",
        shares=Decimal(shares),
        amount=Decimal(amount),
        linked_transaction_id=linked_transaction_id,
    )


def test_list_transactions_filters_and_orders_by_trade_date_created_at_and_id(repo):
    repo.add_product(product("alpha"))
    repo.add_product(product("beta"))
    rows = [
        tx(
            "z-last-id",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "a-first-id",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_CONFIRMATION,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "older-created",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "older-trade",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 29),
        ),
        tx(
            "other-product",
            "beta",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 28),
        ),
    ]
    for row in rows:
        repo.create_transaction(row)
    with repo.database.transaction() as conn:
        conn.execute(
            """UPDATE transactions
               SET created_at = CASE
                   WHEN id = 'older-created' THEN '2026-07-30 00:00:00'
                   ELSE '2026-07-30 01:00:00'
               END
               WHERE product_id = 'alpha'"""
        )

    assert [
        row.id for row in repo.list_transactions(product_id="alpha")
    ] == ["older-trade", "older-created", "a-first-id", "z-last-id"]
    assert [
        row.id
        for row in repo.list_transactions(
            statuses=[TransactionStatus.PENDING_CONFIRMATION]
        )
    ] == ["a-first-id"]
    assert repo.list_transactions(statuses=[]) == []


def test_projector_replays_confirmed_events_and_pending_locks(repo):
    repo.add_product(product("cash"))
    repo.create_transaction(
        tx(
            "open",
            "cash",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "10000",
            "10000",
        )
    )
    repo.create_transaction(
        tx(
            "pending-redemption",
            "cash",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.PENDING_CONFIRMATION,
            "1500",
            "1500",
        )
    )
    repo.create_transaction(
        tx(
            "pending-transfer",
            "cash",
            TransactionType.CASH_TRANSFER_OUT,
            TransactionStatus.PENDING_QUOTE,
            "500",
            "500",
        )
    )
    repo.create_transaction(
        tx(
            "pending-purchase",
            "cash",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "9000",
            "9000",
        )
    )

    position = PositionProjector(repo).calculate("cash")

    assert position == Position(
        "cash",
        available_shares=Decimal("8000"),
        locked_shares=Decimal("2000"),
        total_shares=Decimal("10000"),
        cost_basis=Decimal("10000"),
    )


def test_projector_uses_average_cost_for_outflows_and_signed_adjustments(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "200",
        ),
        tx(
            "02-buy",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.CONFIRMED,
            "100",
            "400",
        ),
        tx(
            "03-redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "50",
            "999",
        ),
        tx(
            "04-adjust-up",
            "fund",
            TransactionType.HOLDING_ADJUSTMENT,
            TransactionStatus.CONFIRMED,
            "30",
            "90",
        ),
        tx(
            "05-adjust-down",
            "fund",
            TransactionType.HOLDING_ADJUSTMENT,
            TransactionStatus.CONFIRMED,
            "-20",
            "999",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("160")
    assert position.cost_basis == Decimal("480")


def test_projector_applies_income_and_transfer_cost_and_ignores_cash_dividend(repo):
    repo.add_product(product("cash"))
    events = [
        tx(
            "01-open",
            "cash",
            TransactionType.CASH_TRANSFER_IN,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
        ),
        tx(
            "02-income",
            "cash",
            TransactionType.INCOME_ACCRUAL,
            TransactionStatus.CONFIRMED,
            "20",
            "20",
        ),
        tx(
            "03-dividend",
            "cash",
            TransactionType.CASH_DIVIDEND,
            TransactionStatus.CONFIRMED,
            "0",
            "500",
        ),
        tx(
            "04-transfer-out",
            "cash",
            TransactionType.CASH_TRANSFER_OUT,
            TransactionStatus.CONFIRMED,
            "30",
            "999",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    assert PositionProjector(repo).calculate("cash") == Position(
        "cash",
        available_shares=Decimal("90"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("90"),
        cost_basis=Decimal("90"),
    )


def test_projector_replays_reversed_original_and_independent_signed_reversal(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "01-original",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.REVERSED,
            "12.5",
            "25",
        )
    )
    repo.create_transaction(
        tx(
            "02-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-12.5",
            "-25",
        )
    )
    repo.create_transaction(
        tx(
            "03-cancelled",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.CANCELLED,
            "100",
            "100",
        )
    )
    repo.create_transaction(
        tx(
            "04-failed",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.FAILED,
            "100",
            "100",
        )
    )

    assert PositionProjector(repo).calculate("fund") == Position(
        "fund",
        available_shares=Decimal("0"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("0"),
        cost_basis=Decimal("0"),
    )


def test_positive_signed_reversal_restores_a_reversed_outflow(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "200",
        ),
        tx(
            "02-outflow",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.REVERSED,
            "20",
            "50",
        ),
        tx(
            "03-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "20",
            "40",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("100")
    assert position.cost_basis == Decimal("200")


def test_linked_reversal_restores_cost_after_a_full_outflow(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "3",
            "1",
        ),
        tx(
            "02-outflow",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.REVERSED,
            "3",
            "10",
        ),
        tx(
            "03-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "3",
            "-10",
            linked_transaction_id="02-outflow",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("3")
    assert position.cost_basis == Decimal("1")


def test_full_average_cost_outflow_has_no_decimal_rounding_residue(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "3",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "02-redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "3",
            "99",
        )
    )

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("0")
    assert position.cost_basis == Decimal("0")


def test_projector_rejects_negative_total_shares(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "1",
        )
    )

    with pytest.raises(ValueError, match="^negative portfolio shares$"):
        PositionProjector(repo).calculate("fund")


def test_projector_rejects_locks_above_total_shares(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "1",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "pending",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.PENDING_QUOTE,
            "1.0000000000000000001",
        )
    )

    with pytest.raises(ValueError, match="^locked shares exceed total shares$"):
        PositionProjector(repo).calculate("fund")


def test_rebuild_replaces_corrupt_projection_from_ledger(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
        )
    )
    repo.replace_position(
        Position(
            "fund",
            Decimal("9"),
            Decimal("0"),
            Decimal("9"),
            Decimal("9"),
        )
    )

    rebuilt = PositionProjector(repo).rebuild("fund")

    assert rebuilt == [
        Position(
            "fund",
            available_shares=Decimal("100"),
            locked_shares=Decimal("0"),
            total_shares=Decimal("100"),
            cost_basis=Decimal("100"),
        )
    ]
    assert repo.get_position("fund") == rebuilt[0]


def test_rebuild_all_is_deterministic_and_persists_only_after_all_calculations(repo):
    repo.add_product(product("a-valid"))
    repo.add_product(product("b-invalid"))
    repo.create_transaction(
        tx(
            "valid-open",
            "a-valid",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "10",
            "20",
        )
    )
    repo.create_transaction(
        tx(
            "invalid-redeem",
            "b-invalid",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "1",
        )
    )
    corrupt = Position(
        "a-valid",
        available_shares=Decimal("9"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("9"),
        cost_basis=Decimal("9"),
    )
    repo.replace_position(corrupt)

    with pytest.raises(ValueError, match="^negative portfolio shares$"):
        PositionProjector(repo).rebuild()

    assert repo.get_position("a-valid") == corrupt
