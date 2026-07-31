from datetime import date
from decimal import Decimal
import threading

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_income import CashIncomeService
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository


INCOME_DATE = date(2026, 7, 29)


@pytest.fixture
def income_services(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    projector = PositionProjector(repository)
    return repository, projector, CashIncomeService(repository, projector)


def seed_product(repository, product_id, product_type):
    repository.add_product(
        Product(
            id=product_id,
            provider="test",
            code=product_id,
            name=product_id,
            product_type=product_type,
        )
    )


def seed_cash(repository, product_id, shares):
    seed_product(repository, product_id, ProductType.CASH_MANAGEMENT)
    repository.create_transaction(
        Transaction(
            id=f"opening:{product_id}",
            product_id=product_id,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 1),
            idempotency_key=f"opening:{product_id}",
            amount=Decimal(shares),
            shares=Decimal(shares),
            confirmation_nav=Decimal("1"),
            confirmation_date=date(2026, 7, 1),
        )
    )
    PositionProjector(repository).rebuild(product_id)


def seed_quote(repository, product_id, quote_date, income_per_10k):
    repository.upsert_quote(
        product_id,
        MarketQuote(
            product_code=product_id,
            quote_date=quote_date,
            source="test",
            raw_hash=f"{product_id}:{quote_date.isoformat()}",
            income_per_10k=(
                None
                if income_per_10k is None
                else Decimal(income_per_10k)
            ),
        ),
        "2026-07-30T12:00:00+08:00",
    )


def test_cash_income_uses_effective_shares_and_compounds_next_day(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    repository.create_transaction(
        Transaction(
            id="locked:cash",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=INCOME_DATE,
            idempotency_key="locked:cash",
            amount=Decimal("2000"),
            shares=Decimal("2000"),
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    first = income.accrue("cash", INCOME_DATE)

    assert first.amount == Decimal("0.4")
    assert first.shares == Decimal("0.4")
    assert projector.calculate("cash").total_shares == Decimal("10000.4")

    next_date = date(2026, 7, 30)
    seed_quote(repository, "cash", next_date, "0.5")
    second = income.accrue("cash", next_date)

    assert second.shares == Decimal("0.40002")


def test_same_day_cash_purchase_starts_earning_on_next_day(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    repository.create_transaction(
        Transaction(
            id="same-day-purchase:cash",
            product_id="cash",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=INCOME_DATE,
            confirmation_date=INCOME_DATE,
            idempotency_key="same-day-purchase:cash",
            amount=Decimal("5000"),
            shares=Decimal("5000"),
            confirmation_nav=Decimal("1"),
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    first = income.accrue("cash", INCOME_DATE)

    assert first.amount == Decimal("0.5")

    next_date = date(2026, 7, 30)
    seed_quote(repository, "cash", next_date, "0.5")
    second = income.accrue("cash", next_date)

    assert second.amount == Decimal("0.750025")


def test_future_pending_cash_out_does_not_reduce_earlier_income(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    repository.create_transaction(
        Transaction(
            id="future-locked:cash",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=date(2026, 8, 10),
            idempotency_key="future-locked:cash",
            amount=Decimal("2000"),
            shares=Decimal("2000"),
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    result = income.accrue("cash", INCOME_DATE)

    assert result.amount == Decimal("0.5")


def test_duplicate_accrual_returns_existing_event(income_services):
    repository, _projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    first = income.accrue("cash", INCOME_DATE)
    second = income.accrue("cash", INCOME_DATE)

    assert second.id == first.id
    assert len(repository.list_transactions("cash")) == 2


def test_missing_exact_income_quote_remains_retryable(income_services):
    repository, _projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", date(2026, 7, 28), "0.5")

    assert income.accrue("cash", INCOME_DATE) is None
    seed_quote(repository, "cash", INCOME_DATE, None)
    assert income.accrue("cash", INCOME_DATE) is None
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    assert income.accrue("cash", INCOME_DATE).shares == Decimal("0.5")


def test_zero_and_negative_income_are_recorded_when_holding_stays_nonnegative(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", INCOME_DATE, "0")
    zero = income.accrue("cash", INCOME_DATE)
    next_date = date(2026, 7, 30)
    seed_quote(repository, "cash", next_date, "-1")
    negative = income.accrue("cash", next_date)

    assert zero.shares == Decimal("0")
    assert negative.shares == Decimal("-1")
    assert projector.calculate("cash").total_shares == Decimal("9999")


def test_income_rejects_non_cash_product(income_services):
    repository, _projector, income = income_services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, "fund", INCOME_DATE, "0.5")

    with pytest.raises(ValueError, match="cash_management"):
        income.accrue("fund", INCOME_DATE)


def test_negative_income_cannot_make_total_shares_negative(income_services):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "1")
    seed_quote(repository, "cash", INCOME_DATE, "-20000")

    with pytest.raises(ValueError, match="negative portfolio shares"):
        income.accrue("cash", INCOME_DATE)

    assert repository.get_transaction_by_idempotency(
        "income:cash:2026-07-29"
    ) is None
    assert projector.calculate("cash").total_shares == Decimal("1")


def test_same_day_concurrent_accrual_creates_one_event(income_services):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def accrue(service):
        try:
            barrier.wait()
            results.append(service.accrue("cash", INCOME_DATE))
        except BaseException as exc:
            errors.append(exc)

    second_service = CashIncomeService(repository, projector)
    threads = [
        threading.Thread(target=accrue, args=(income,)),
        threading.Thread(target=accrue, args=(second_service,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len({result.id for result in results}) == 1
    assert projector.calculate("cash").total_shares == Decimal("10000.5")


def test_accrual_and_position_rebuild_share_one_transaction(
    income_services,
    monkeypatch,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    def fail_replace(_position, conn=None):
        assert conn is not None
        raise RuntimeError("projection failed")

    monkeypatch.setattr(repository, "replace_position", fail_replace)

    with pytest.raises(RuntimeError, match="projection failed"):
        income.accrue("cash", INCOME_DATE)

    assert repository.get_transaction_by_idempotency(
        "income:cash:2026-07-29"
    ) is None
