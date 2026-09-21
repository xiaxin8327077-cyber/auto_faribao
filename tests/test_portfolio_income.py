from datetime import date, datetime
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


def seed_cash(repository, product_id, shares, provider="test"):
    repository.add_product(
        Product(
            id=product_id,
            provider=provider,
            code=product_id,
            name=product_id,
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
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


def test_purchase_confirmed_on_same_day_starts_earning_that_day(
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

    assert first.amount == Decimal("0.75")

    next_date = date(2026, 7, 30)
    seed_quote(repository, "cash", next_date, "0.5")
    second = income.accrue("cash", next_date)

    assert second.amount == Decimal("0.7500375")


def test_purchase_pending_confirmation_does_not_earn_until_confirmed(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    repository.create_transaction(
        Transaction(
            id="pending-purchase:cash",
            product_id="cash",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=INCOME_DATE,
            idempotency_key="pending-purchase:cash",
            amount=Decimal("5000"),
            shares=Decimal("5000"),
            confirmation_nav=Decimal("1"),
            trade_time=f"{INCOME_DATE.isoformat()}T10:00:00",
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    before_confirm = income.accrue("cash", INCOME_DATE)

    assert before_confirm.amount == Decimal("0.5")
    assert projector.calculate("cash").total_shares == Decimal("15000.5")


def test_purchase_earns_from_day_after_wallet_confirmation(
    income_services,
):
    """钱包 T+1 确认：确认日收益按前一交易日份额；次日收益日起息。"""
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    confirm_date = date(2026, 7, 30)
    next_day = date(2026, 7, 31)
    repository.create_transaction(
        Transaction(
            id="t1-purchase:cash",
            product_id="cash",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=INCOME_DATE,
            confirmation_date=confirm_date,
            idempotency_key="t1-purchase:cash",
            amount=Decimal("5000"),
            shares=Decimal("5000"),
            confirmation_nav=Decimal("1"),
            trade_time=f"{INCOME_DATE.isoformat()}T10:00:00",
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")
    seed_quote(repository, "cash", confirm_date, "0.5")
    seed_quote(repository, "cash", next_day, "0.5")

    trade_day = income.accrue("cash", INCOME_DATE)
    confirm_day = income.accrue("cash", confirm_date)
    earn_day = income.accrue("cash", next_day)

    assert trade_day.amount == Decimal("0.5")
    assert confirm_day.amount == Decimal("0.500025")
    assert earn_day.amount == Decimal("0.75005000125")


def test_wallet_outflow_can_consume_future_confirming_liquidity_without_negative_history(
    income_services,
):
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    repository.create_transaction(
        Transaction(
            id="future-confirming-purchase:cash",
            product_id="cash",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=INCOME_DATE,
            confirmation_date=date(2026, 7, 31),
            idempotency_key="future-confirming-purchase:cash",
            amount=Decimal("5000"),
            shares=Decimal("5000"),
            confirmation_nav=Decimal("1"),
        )
    )
    repository.create_transaction(
        Transaction(
            id="same-day-outflow:cash",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.CONFIRMED,
            trade_date=INCOME_DATE,
            confirmation_date=INCOME_DATE,
            idempotency_key="same-day-outflow:cash",
            amount=Decimal("12000"),
            shares=Decimal("12000"),
            confirmation_nav=Decimal("1"),
        )
    )
    projector.rebuild("cash")
    seed_quote(repository, "cash", date(2026, 7, 30), "0.5")

    result = income.accrue("cash", date(2026, 7, 30))

    assert result.amount == Decimal("0")
    assert projector.calculate_confirmed_as_of(
        "cash", INCOME_DATE
    ).total_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("3000")


def test_wallet_income_accrues_on_weekend(income_services):
    """现金产品周末也计提收益。"""
    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    saturday = date(2026, 8, 1)
    seed_quote(repository, "cash", saturday, "0.5")

    result = income.accrue("cash", saturday)

    assert result is not None
    assert result.amount == Decimal("0.5")
    assert projector.calculate("cash").total_shares == Decimal("10000.5")


def test_cash_income_dates_to_accrue_covers_fri_sat_sun_on_monday():
    """周一（如 8/10）补齐到昨天：周五、周六、周日（最新为 8/9）。"""
    from src.portfolio_wallet import cash_income_dates_to_accrue

    assert cash_income_dates_to_accrue(date(2026, 8, 10), lookback_days=2) == [
        date(2026, 8, 7),
        date(2026, 8, 8),
        date(2026, 8, 9),
    ]


def test_wallet_monday_accrues_friday_saturday_sunday(income_services, monkeypatch):
    """周一连续计提时，周五、周六、周日三天都应入账，不含周一。"""
    from datetime import datetime

    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    for day in (date(2026, 8, 7), date(2026, 8, 8), date(2026, 8, 9)):
        seed_quote(repository, "cash", day, "0.5")
    monkeypatch.setattr(
        "src.portfolio_income.beijing_now",
        lambda: datetime(2026, 8, 10, 9, 0),
    )
    from src.portfolio_wallet import cash_income_dates_to_accrue

    booked = []
    for target in cash_income_dates_to_accrue(date(2026, 8, 10), lookback_days=2):
        result = income.accrue("cash", target)
        assert result is not None
        booked.append((target, result.amount))

    assert [day for day, _ in booked] == [
        date(2026, 8, 7),
        date(2026, 8, 8),
        date(2026, 8, 9),
    ]
    assert income.accrue("cash", date(2026, 8, 10)) is None
    assert booked[0][1] == Decimal("0.5")
    assert booked[1][1] == Decimal("0.500025")
    assert booked[2][1] == Decimal("0.50005000125")
    assert projector.calculate("cash").total_shares == Decimal("10001.50007500125")


def test_cash_latest_profit_bundles_friday_saturday_sunday(income_services, monkeypatch):
    """最新收益在周一应显示周五+周六+周日合计，日期仍为周日。"""
    from datetime import datetime
    from src.portfolio_profit import calculate_latest_profit
    from src.portfolio_wallet import cash_income_dates_to_accrue

    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    for day in (date(2026, 8, 7), date(2026, 8, 8), date(2026, 8, 9)):
        seed_quote(repository, "cash", day, "0.5")
    monkeypatch.setattr(
        "src.portfolio_income.beijing_now",
        lambda: datetime(2026, 8, 10, 9, 0),
    )
    for target in cash_income_dates_to_accrue(date(2026, 8, 10), lookback_days=2):
        income.accrue("cash", target)

    product = repository.require_product("cash")
    profit_date, profit = calculate_latest_profit(
        repository, product, date(2026, 8, 10)
    )
    assert profit_date == date(2026, 8, 9)
    assert profit == Decimal("1.50007500125")

    single_date, single = calculate_latest_profit(
        repository,
        product,
        date(2026, 8, 9),
        bundle_non_trading_days=False,
    )
    assert single_date == date(2026, 8, 9)
    assert single == Decimal("0.50005000125")


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


def test_wallet_plus_redemption_stops_earning_after_trade_date(
    income_services,
    monkeypatch,
):
    """钱包Plus 赎回当天到账后，次日收益不能再按赎回前份额计息。"""
    from src.portfolio_transactions import PortfolioTransactionService
    from src.portfolio_wallet import WALLET_PROVIDER

    repository, projector, income = income_services
    seed_cash(repository, "wallet", "10000", provider=WALLET_PROVIDER)
    transactions = PortfolioTransactionService(repository, projector)
    transactions.record_redemption(
        "wallet",
        Decimal("4000"),
        date(2026, 8, 18),
        "web:wallet-plus-redemption-income",
        trade_time="2026-08-18T10:54:00",
    )
    seed_quote(repository, "wallet", date(2026, 8, 18), "0.5")
    seed_quote(repository, "wallet", date(2026, 8, 19), "0.5")
    monkeypatch.setattr(
        "src.portfolio_income.beijing_now",
        lambda: datetime(2026, 8, 20, 9, 0),
    )

    trade_day = income.accrue("wallet", date(2026, 8, 18))
    next_day = income.accrue("wallet", date(2026, 8, 19))

    assert trade_day.amount == Decimal("0.5")
    assert next_day.amount == Decimal("0.300025")
    assert projector.calculate("wallet").total_shares == Decimal("6000.800025")
    assert projector.calculate("wallet").locked_shares == Decimal("0")


def test_duplicate_accrual_returns_existing_event(income_services):
    repository, _projector, income = income_services
    seed_cash(repository, "cash", "10000")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")

    first = income.accrue("cash", INCOME_DATE)
    second = income.accrue("cash", INCOME_DATE)

    assert second.id == first.id
    assert len(repository.list_transactions("cash")) == 2


def test_reversed_wallet_accrual_can_reactivate_multiple_times(
    income_services,
):
    from src.portfolio_transactions import PortfolioTransactionService

    repository, projector, income = income_services
    seed_cash(repository, "cash", "10000", provider="wallet_plus")
    seed_quote(repository, "cash", INCOME_DATE, "0.5")
    tx = PortfolioTransactionService(repository, projector)

    first = income.accrue("cash", INCOME_DATE)
    assert first is not None
    tx.reverse_confirmed(
        first.id,
        "correct premature accrual",
        "reverse:income:cash:1",
        actor="ops",
    )
    second = income.accrue("cash", INCOME_DATE)
    assert second is not None
    assert second.id != first.id
    assert second.status is TransactionStatus.CONFIRMED
    assert second.idempotency_key.endswith(":reactivated")

    tx.reverse_confirmed(
        second.id,
        "correct again",
        "reverse:income:cash:2",
        actor="ops",
    )
    third = income.accrue("cash", INCOME_DATE)
    assert third is not None
    assert third.id != second.id
    assert third.status is TransactionStatus.CONFIRMED
    assert third.idempotency_key.endswith(":reactivated:reactivated")
    assert third.amount == Decimal("0.5")


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
