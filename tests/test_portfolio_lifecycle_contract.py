from datetime import date, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

from src.portfolio_db import PortfolioDatabase
from src.portfolio_jobs import PortfolioCycleResult, build_portfolio_jobs
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
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_view import (
    build_portfolio_payload,
    build_product_history_payload,
)


WALLET = "wallet-plus"
SOURCE_FUND = "source-fund"
TARGET_FUND = "target-fund"
TRADE_DAY = date(2026, 9, 18)
CONFIRM_DAY = date(2026, 9, 21)
ZERO = Decimal("0")


def _add_product(
    repository,
    product_id,
    provider,
    product_type,
    metadata_json="{}",
):
    repository.add_product(Product(
        product_id,
        provider,
        product_id.upper(),
        product_id,
        product_type,
        metadata_json=metadata_json,
    ))


def _opening(repository, product_id, shares):
    repository.create_transaction(Transaction(
        id=f"opening:{product_id}",
        product_id=product_id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 9, 1),
        confirmation_date=date(2026, 9, 1),
        idempotency_key=f"opening:{product_id}",
        amount=Decimal(shares),
        shares=Decimal(shares),
        confirmation_nav=Decimal("1"),
    ))


def _quote(repository, product_id, quote_date, *, nav=None, income=None):
    repository.upsert_quote(
        product_id,
        MarketQuote(
            product_code=product_id.upper(),
            quote_date=quote_date,
            source="lifecycle-contract",
            raw_hash=(
                f"{product_id}:{quote_date.isoformat()}:{nav}:{income}"
            ),
            unit_nav=None if nav is None else Decimal(nav),
            income_per_10k=None if income is None else Decimal(income),
        ),
        f"{quote_date.isoformat()}T12:00:00+08:00",
    )


def _row(payload, product_id):
    return next(row for row in payload["products"] if row["product_id"] == product_id)


def _audit_rows(repository):
    with repository.database.connection() as conn:
        return [
            tuple(row)
            for row in conn.execute("SELECT * FROM audit_logs ORDER BY id")
        ]


def _run_cycle(jobs, clock, day):
    now = datetime(day.year, day.month, day.day, 18, 0)
    clock["now"] = now
    return jobs.run_cycle(now)


def test_wallet_lifecycle_preserves_assets_and_income_across_settlement_and_restart(
    tmp_path,
    monkeypatch,
):
    """A full wallet/fund lifecycle keeps funds single-counted at every step."""
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    _add_product(repository, WALLET, "wallet_plus", ProductType.CASH_MANAGEMENT)
    _add_product(
        repository,
        SOURCE_FUND,
        "fund",
        ProductType.PUBLIC_FUND,
        metadata_json=json.dumps({
            "trade_rules": {"redemption": {"confirmation_trading_days": 0}},
        }),
    )
    _add_product(repository, TARGET_FUND, "fund", ProductType.PUBLIC_FUND)
    _opening(repository, WALLET, "10000")
    _opening(repository, SOURCE_FUND, "5000")
    projector = PositionProjector(repository)
    projector.rebuild()

    _quote(repository, SOURCE_FUND, TRADE_DAY, nav="1")
    _quote(repository, WALLET, TRADE_DAY, income="0")
    _quote(repository, WALLET, date(2026, 9, 19), income="0.5")
    _quote(repository, WALLET, date(2026, 9, 20), income="0.5")
    _quote(repository, WALLET, CONFIRM_DAY, income="0.5")
    clock = {"now": datetime(2026, 9, 18, 12, 0)}
    monkeypatch.setattr(
        "src.portfolio_income.beijing_now",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "src.portfolio_jobs.QuoteSyncService.sync_product",
        lambda *_args, **_kwargs: [],
    )

    transactions = PortfolioTransactionService(repository, projector)
    runtime = SimpleNamespace(repository=repository, write_enabled=True)
    jobs = build_portfolio_jobs(runtime, strict=True)

    initial = build_portfolio_payload(repository, as_of=TRADE_DAY)
    wallet_initial = _row(initial, WALLET)
    source_initial = _row(initial, SOURCE_FUND)
    assert Decimal(wallet_initial["shares"]) == Decimal("10000")
    assert Decimal(wallet_initial["in_transit_amount"]) == ZERO
    assert Decimal(source_initial["shares"]) == Decimal("5000")
    assert Decimal(source_initial["in_transit_amount"]) == ZERO
    assert Decimal(initial["summary"]["total_assets"]) == Decimal("15000")

    redemption = transactions.record_redemption(
        SOURCE_FUND,
        Decimal("5000"),
        TRADE_DAY,
        "lifecycle:redeem-source",
        destination_cash_product_id=WALLET,
        settlement_date=TRADE_DAY,
    )
    assert redemption.status is TransactionStatus.CONFIRMED

    awaiting_arrival = build_portfolio_payload(repository, as_of=TRADE_DAY)
    wallet_awaiting = _row(awaiting_arrival, WALLET)
    source_awaiting = _row(awaiting_arrival, SOURCE_FUND)
    assert Decimal(wallet_awaiting["shares"]) == Decimal("10000")
    assert Decimal(wallet_awaiting["in_transit_amount"]) == ZERO
    assert Decimal(source_awaiting["shares"]) == ZERO
    assert Decimal(source_awaiting["in_transit_amount"]) == ZERO
    assert redemption.settlement_status.value == "pending"
    assert Decimal(awaiting_arrival["summary"]["total_assets"]) == Decimal("15000")

    result_18 = _run_cycle(jobs, clock, TRADE_DAY)
    assert result_18.redemptions_settled == 1

    wallet_arrival = repository.find_purchase_by_origin(redemption.id)
    assert wallet_arrival is not None
    assert wallet_arrival.status is TransactionStatus.PENDING_CONFIRMATION
    assert wallet_arrival.amount == Decimal("5000")

    arrival_pending = build_portfolio_payload(repository, as_of=TRADE_DAY)
    wallet_pending = _row(arrival_pending, WALLET)
    source_pending = _row(arrival_pending, SOURCE_FUND)
    assert Decimal(wallet_pending["shares"]) == Decimal("15000")
    assert Decimal(wallet_pending["in_transit_amount"]) == Decimal("5000")
    assert Decimal(source_pending["shares"]) == ZERO
    assert Decimal(source_pending["in_transit_amount"]) == ZERO
    assert Decimal(arrival_pending["summary"]["total_assets"]) == Decimal("15000")

    target_purchase = transactions.record_purchase(
        TARGET_FUND,
        Decimal("12000"),
        TRADE_DAY,
        "lifecycle:buy-target",
        source_cash_product_id=WALLET,
        trade_time="2026-09-18T10:11:00",
    )
    assert target_purchase.status is TransactionStatus.PENDING_QUOTE

    before_confirmation = build_portfolio_payload(repository, as_of=TRADE_DAY)
    wallet_before = _row(before_confirmation, WALLET)
    target_before = _row(before_confirmation, TARGET_FUND)
    assert Decimal(wallet_before["shares"]) == Decimal("3000")
    assert Decimal(wallet_before["in_transit_amount"]) == ZERO
    assert Decimal(target_before["in_transit_amount"]) == Decimal("12000")
    assert Decimal(before_confirmation["summary"]["total_assets"]) == Decimal("15000")
    assert projector.calculate_confirmed_as_of(
        WALLET, TRADE_DAY
    ).total_shares == Decimal("3000")

    _quote(repository, TARGET_FUND, TRADE_DAY, nav="1")
    _run_cycle(jobs, clock, date(2026, 9, 19))
    _run_cycle(jobs, clock, date(2026, 9, 20))
    result_21 = _run_cycle(jobs, clock, CONFIRM_DAY)
    assert result_21.trades_settled == 2

    settled_arrival = repository.get_transaction_by_id(wallet_arrival.id)
    settled_target = repository.get_transaction_by_id(target_purchase.id)
    assert settled_arrival.status is TransactionStatus.CONFIRMED
    assert settled_arrival.confirmation_date == CONFIRM_DAY
    assert settled_target.status is TransactionStatus.CONFIRMED
    assert settled_target.confirmation_date == CONFIRM_DAY
    _run_cycle(jobs, clock, date(2026, 9, 22))
    assert projector.calculate_confirmed_as_of(
        WALLET, CONFIRM_DAY
    ).total_shares == Decimal("3000.450022500375")

    incomes = {
        transaction.trade_date: transaction.amount
        for transaction in repository.list_transactions(product_id=WALLET)
        if transaction.transaction_type is TransactionType.INCOME_ACCRUAL
    }
    assert incomes[date(2026, 9, 19)] == Decimal("0.15")
    assert incomes[date(2026, 9, 20)] == Decimal("0.1500075")
    assert incomes[CONFIRM_DAY] == Decimal("0.150015000375")

    final_payload = build_portfolio_payload(repository, as_of=CONFIRM_DAY)
    wallet_final = _row(final_payload, WALLET)
    target_final = _row(final_payload, TARGET_FUND)
    assert Decimal(wallet_final["shares"]) == Decimal("3000.450022500375")
    assert Decimal(target_final["shares"]) == Decimal("12000")
    assert Decimal(final_payload["summary"]["total_assets"]) == (
        Decimal("15000.450022500375")
    )

    history = build_product_history_payload(
        repository,
        WALLET,
        as_of=CONFIRM_DAY,
    )["history"]
    daily_profits = {
        date.fromisoformat(row["date"]): Decimal(row["profit"])
        for row in history
        if row["profit"] is not None
    }
    assert daily_profits[date(2026, 9, 19)] == Decimal("0.15")
    assert daily_profits[date(2026, 9, 20)] == Decimal("0.1500075")
    assert daily_profits[CONFIRM_DAY] == Decimal("0.150015000375")

    before_transactions = repository.list_transactions()
    before_positions = {
        product.id: repository.get_position(product.id)
        for product in repository.list_products()
    }
    before_audit = _audit_rows(repository)
    same_slot = jobs.run_cycle(datetime(2026, 9, 22, 18, 20))
    restarted_database = PortfolioDatabase(database.path)
    restarted_repository = PortfolioRepository(restarted_database)
    restarted_runtime = SimpleNamespace(
        repository=restarted_repository,
        write_enabled=True,
    )
    restarted_jobs = build_portfolio_jobs(restarted_runtime, strict=True)
    restarted_jobs.run_cycle(datetime(2026, 9, 22, 20, 0))
    assert same_slot == PortfolioCycleResult(0, 0, 0, 0, 0)
    assert restarted_repository.list_transactions() == before_transactions
    assert {
        product.id: restarted_repository.get_position(product.id)
        for product in restarted_repository.list_products()
    } == before_positions
    assert _audit_rows(restarted_repository) == before_audit
    assert (
        build_portfolio_payload(restarted_repository, as_of=CONFIRM_DAY)
        == final_payload
    )
