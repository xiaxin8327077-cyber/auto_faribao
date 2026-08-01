from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductType,
    SipPlan,
    SipPlanStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_sip import PlanExecution
from src.portfolio_view import (
    build_portfolio_payload,
    build_product_history_payload,
)


@pytest.fixture
def portfolio_fixture(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    products = (
        Product("wealth", "citic_wealth", "AF233276B", "净值理财", ProductType.WEALTH_NAV),
        Product("cash", "nanyin_wealth", "NYRR000007", "现金管理", ProductType.CASH_MANAGEMENT),
        Product("fund", "fund", "003103", "公募基金", ProductType.PUBLIC_FUND),
    )
    for product in products:
        repository.add_product(product)
        repository.create_transaction(
            Transaction(
                id=f"opening:{product.id}",
                product_id=product.id,
                transaction_type=TransactionType.OPENING_POSITION,
                status=TransactionStatus.CONFIRMED,
                trade_date=date(2026, 7, 1),
                idempotency_key=f"opening:{product.id}",
                amount=Decimal("100"),
                shares=Decimal("100"),
            )
        )
    repository.upsert_quote(
        "wealth",
        MarketQuote("AF233276B", date(2026, 7, 30), "official", "wealth-hash", unit_nav=Decimal("1.078")),
        "2026-07-30T10:00:00",
    )
    repository.upsert_quote(
        "cash",
        MarketQuote(
            "NYRR000007", date(2026, 7, 30), "official", "cash-hash",
            income_per_10k=Decimal("0.4475"),
            seven_day_annualized_rate=Decimal("0.016315"),
        ),
        "2026-07-30T10:00:00",
    )
    repository.upsert_quote(
        "fund",
        MarketQuote("003103", date(2026, 7, 30), "official", "fund-hash", unit_nav=Decimal("1.0321")),
        "2026-07-30T10:00:00",
    )
    repository.create_transaction(
        Transaction(
            id="income:cash:2026-07-30",
            product_id="cash",
            transaction_type=TransactionType.INCOME_ACCRUAL,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 30),
            idempotency_key="income:cash:2026-07-30",
            amount=Decimal("0.4475"),
            shares=Decimal("0.4475"),
        )
    )
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO legacy_profit_history
               (id, profit_date, amount, source_kind, source_json)
               VALUES (?, ?, ?, ?, ?)""",
            ("legacy:manual", "2026-07-28", "9.87", "manual", "{}"),
        )
    PositionProjector(repository).rebuild()
    return repository


def test_payload_keeps_type_specific_quote_semantics(portfolio_fixture):
    repo = portfolio_fixture
    payload = build_portfolio_payload(repo, as_of=date(2026, 7, 30))
    rows = {row["code"]: row for row in payload["products"]}

    assert rows["AF233276B"]["quote"]["unit_nav"] == "1.078"
    assert "income_per_10k" not in rows["AF233276B"]["quote"]
    assert rows["NYRR000007"]["quote"]["income_per_10k"] == "0.4475"
    assert rows["NYRR000007"]["quote"]["seven_day_annualized_rate"] == "0.016315"
    assert "unit_nav" not in rows["NYRR000007"]["quote"]
    assert rows["003103"]["quote"]["unit_nav"] == "1.0321"


def test_payload_preserves_legacy_profit_rows_without_recalculation(portfolio_fixture):
    payload = build_portfolio_payload(portfolio_fixture)

    assert payload["profit_history"][0] == {
        "date": "2026-07-28",
        "amount": "9.87",
        "source": "legacy_manual",
    }


def test_payload_keeps_overview_fields_and_cash_value_is_shares(portfolio_fixture):
    payload = build_portfolio_payload(portfolio_fixture, as_of=date(2026, 7, 30))
    cash = next(row for row in payload["products"] if row["code"] == "NYRR000007")

    assert cash["shares"] == "100.4475"
    assert cash["latest_nav"] is None
    assert cash["market_value"] == "100.4475"
    assert cash["latest_profit"] == "0.4475"
    assert cash["latest_profit_date"] == "2026-07-30"


def test_cash_latest_profit_date_stays_on_latest_income_not_newer_quote(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.upsert_quote(
        "cash",
        MarketQuote(
            "NYRR000007",
            date(2026, 7, 31),
            "official",
            "cash-newer-quote",
            income_per_10k=Decimal("0.4500"),
            seven_day_annualized_rate=Decimal("0.0164"),
        ),
        "2026-07-31T10:00:00",
    )

    cash = next(
        row
        for row in build_portfolio_payload(
            repository,
            as_of=date(2026, 7, 31),
        )["products"]
        if row["code"] == "NYRR000007"
    )

    assert cash["quote"]["date"] == "2026-07-31"
    assert cash["latest_profit_date"] == "2026-07-30"
    assert cash["latest_profit"] == "0.4475"


def test_non_cash_product_row_includes_latest_quote_change(portfolio_fixture):
    repository = portfolio_fixture
    repository.upsert_quote(
        "wealth",
        MarketQuote(
            "AF233276B",
            date(2026, 7, 29),
            "official",
            "wealth-previous",
            unit_nav=Decimal("1.070"),
        ),
        "2026-07-29T10:00:00",
    )

    payload = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 30),
    )
    wealth = next(
        row for row in payload["products"] if row["code"] == "AF233276B"
    )

    assert wealth["change_pct"] == "0.7476635514018691588785046729"


def test_product_history_lists_nav_date_change_and_daily_profit_newest_first(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.upsert_quote(
        "wealth",
        MarketQuote(
            "AF233276B",
            date(2026, 7, 29),
            "official",
            "wealth-previous",
            unit_nav=Decimal("1.070"),
        ),
        "2026-07-29T10:00:00",
    )

    payload = build_product_history_payload(
        repository,
        "wealth",
        as_of=date(2026, 7, 30),
    )

    assert payload["product"] == {
        "id": "wealth",
        "code": "AF233276B",
        "name": "净值理财",
        "product_type": "wealth_nav",
    }
    assert payload["history"] == [
        {
            "date": "2026-07-30",
            "unit_nav": "1.078",
            "change_pct": "0.7476635514018691588785046729",
            "profit": "0.8",
        },
        {
            "date": "2026-07-29",
            "unit_nav": "1.07",
            "change_pct": None,
            "profit": None,
        },
    ]


def test_cash_product_history_lists_yield_date_and_recorded_income(
    portfolio_fixture,
):
    payload = build_product_history_payload(
        portfolio_fixture,
        "cash",
        as_of=date(2026, 7, 30),
    )

    assert payload["history"] == [
        {
            "date": "2026-07-30",
            "income_per_10k": "0.4475",
            "seven_day_yield": "0.016315",
            "profit": "0.4475",
        }
    ]


def test_transactions_are_newest_first_and_show_cash_route(portfolio_fixture):
    repository = portfolio_fixture
    service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    purchase = service.record_purchase(
        "fund",
        Decimal("10"),
        date(2026, 7, 30),
        "web:purchase-with-source",
        source_cash_product_id="cash",
        trade_time="2026-07-30T14:00:00",
    )
    redemption = service.record_redemption(
        "fund",
        Decimal("5"),
        date(2026, 7, 30),
        "web:redemption-with-destination",
        destination_cash_product_id="cash",
        trade_time="2026-07-30T14:30:00",
    )

    rows = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 30),
    )["transactions"]
    business_rows = [
        row for row in rows if row["id"] in {purchase.id, redemption.id}
    ]

    assert [row["id"] for row in business_rows] == [
        redemption.id,
        purchase.id,
    ]
    assert business_rows[0]["redemption_destination"] == "现金管理"
    assert business_rows[1]["funding_source"] == "现金管理"


def test_external_cash_route_is_named_wallet(portfolio_fixture):
    repository = portfolio_fixture
    service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    purchase = service.record_purchase(
        "fund",
        Decimal("10"),
        date(2026, 7, 30),
        "web:purchase-from-wallet",
        trade_time="2026-07-30T13:00:00",
    )
    redemption = service.record_redemption(
        "fund",
        Decimal("5"),
        date(2026, 7, 30),
        "web:redemption-to-wallet",
        trade_time="2026-07-30T13:30:00",
    )

    rows = {
        row["id"]: row
        for row in build_portfolio_payload(repository)["transactions"]
    }

    assert rows[purchase.id]["funding_source"] == "钱包"
    assert rows[redemption.id]["redemption_destination"] == "钱包"


def test_position_row_shows_available_shares_and_pending_purchase_amount(
    portfolio_fixture,
):
    repository = portfolio_fixture
    for transaction in (
        Transaction(
            id="pending-manual-purchase",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.PENDING_QUOTE,
            trade_date=date(2026, 7, 30),
            idempotency_key="pending-manual-purchase",
            amount=Decimal("25"),
        ),
        Transaction(
            id="pending-sip-purchase",
            product_id="fund",
            transaction_type=TransactionType.SIP_PURCHASE,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=date(2026, 7, 30),
            idempotency_key="pending-sip-purchase",
            amount=Decimal("10"),
        ),
        Transaction(
            id="pending-redemption",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=date(2026, 7, 30),
            idempotency_key="pending-redemption",
            shares=Decimal("5"),
        ),
        Transaction(
            id="cancelled-purchase",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CANCELLED,
            trade_date=date(2026, 7, 30),
            idempotency_key="cancelled-purchase",
            amount=Decimal("99"),
        ),
    ):
        repository.create_transaction(transaction)
    PositionProjector(repository).rebuild("fund")

    row = next(
        item
        for item in build_portfolio_payload(repository)["positions"]
        if item["product_id"] == "fund"
    )

    assert row["shares"] == "100"
    assert row["available_shares"] == "95"
    assert row["locked_shares"] == "5"
    assert row["in_transit_amount"] == "35"


def test_pending_purchase_without_confirmed_shares_is_visible_as_position(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.add_product(
        Product(
            "pending-fund",
            "fund",
            "400030",
            "东方添益债券",
            ProductType.PUBLIC_FUND,
        )
    )
    repository.create_transaction(
        Transaction(
            id="pending-only-purchase",
            product_id="pending-fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=date(2026, 7, 30),
            idempotency_key="pending-only-purchase",
            amount=Decimal("50000"),
        )
    )

    row = next(
        item
        for item in build_portfolio_payload(repository)["positions"]
        if item["product_id"] == "pending-fund"
    )

    assert row["shares"] == "0"
    assert row["available_shares"] == "0"
    assert row["in_transit_amount"] == "50000"


def test_confirmed_full_redemption_removes_product_from_positions_only(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.create_transaction(
        Transaction(
            id="redeem-all:wealth",
            product_id="wealth",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 30),
            confirmation_date=date(2026, 7, 30),
            idempotency_key="redeem-all:wealth",
            amount=Decimal("107.8"),
            shares=Decimal("100"),
            confirmation_nav=Decimal("1.078"),
        )
    )
    PositionProjector(repository).rebuild()

    payload = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 30),
    )

    assert "wealth" in {
        row["product_id"] for row in payload["products"]
    }
    assert "wealth" not in {
        row["product_id"] for row in payload["positions"]
    }


@pytest.mark.parametrize(
    ("settlement_date", "as_of", "is_visible"),
    (
        (date(2026, 7, 31), date(2026, 7, 30), True),
        (date(2026, 7, 31), date(2026, 7, 31), True),
        (date(2026, 7, 31), date(2026, 8, 1), False),
    ),
)
def test_confirmed_full_redemption_is_visible_until_settlement_date(
    portfolio_fixture, settlement_date, as_of, is_visible
):
    repository = portfolio_fixture
    repository.create_transaction(
        Transaction(
            id="redeem-all:wealth:settlement",
            product_id="wealth",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 30),
            confirmation_date=date(2026, 7, 30),
            settlement_date=settlement_date,
            idempotency_key="redeem-all:wealth:settlement",
            amount=Decimal("107.8"),
            shares=Decimal("100"),
            confirmation_nav=Decimal("1.078"),
        )
    )
    PositionProjector(repository).rebuild()

    payload = build_portfolio_payload(repository, as_of=as_of)
    position_ids = {row["product_id"] for row in payload["positions"]}
    redemption = next(
        row
        for row in payload["transactions"]
        if row["id"] == "redeem-all:wealth:settlement"
    )

    assert ("wealth" in position_ids) is is_visible
    assert redemption["settlement_date"] == "2026-07-31"


def test_public_fund_profit_uses_confirmed_manual_and_sip_shares(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    product = Product(
        "fund",
        "changsheng_fund",
        "003103",
        "公募基金",
        ProductType.PUBLIC_FUND,
    )
    repository.add_product(product)
    for transaction in (
        Transaction(
            id="manual",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 29),
            confirmation_date=date(2026, 7, 30),
            idempotency_key="manual",
            amount=Decimal("100"),
            shares=Decimal("100"),
        ),
        Transaction(
            id="sip",
            product_id="fund",
            transaction_type=TransactionType.SIP_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 30),
            confirmation_date=date(2026, 7, 31),
            idempotency_key="sip",
            amount=Decimal("50"),
            shares=Decimal("50"),
        ),
        Transaction(
            id="redeem",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 31),
            confirmation_date=date(2026, 8, 3),
            idempotency_key="redeem",
            amount=Decimal("20"),
            shares=Decimal("20"),
        ),
        Transaction(
            id="same-day-purchase",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 8, 4),
            confirmation_date=date(2026, 8, 4),
            idempotency_key="same-day-purchase",
            amount=Decimal("13"),
            shares=Decimal("10"),
        ),
        Transaction(
            id="pending-redemption",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.PENDING_QUOTE,
            trade_date=date(2026, 8, 4),
            idempotency_key="pending-redemption",
            shares=Decimal("10"),
        ),
    ):
        repository.create_transaction(transaction)
    for quote_date, nav in (
        (date(2026, 7, 30), "1.0"),
        (date(2026, 7, 31), "1.1"),
        (date(2026, 8, 3), "1.2"),
        (date(2026, 8, 4), "1.3"),
    ):
        repository.upsert_quote(
            "fund",
            MarketQuote(
                "003103",
                quote_date,
                "official",
                f"quote:{quote_date}",
                unit_nav=Decimal(nav),
            ),
            "2026-08-04T20:00:00",
        )
    PositionProjector(repository).rebuild()

    sip_confirmation_payload = build_portfolio_payload(
        repository, as_of=date(2026, 7, 31)
    )
    profit_on_sip_confirmation = sip_confirmation_payload["products"][0][
        "latest_profit"
    ]
    profit_on_redemption_confirmation = build_portfolio_payload(
        repository, as_of=date(2026, 8, 3)
    )["products"][0]["latest_profit"]
    profit_after_redemption = build_portfolio_payload(
        repository, as_of=date(2026, 8, 4)
    )["products"][0]["latest_profit"]

    assert profit_on_sip_confirmation == "15"
    assert sip_confirmation_payload["products"][0]["holding_profit"] == "15"
    assert profit_on_redemption_confirmation == "15"
    assert profit_after_redemption == "13"

    service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    service.adjust_latest_profit(
        "fund",
        Decimal("7.5"),
        date(2026, 8, 4),
        "校准最新收益",
        "web:latest-profit-consistency",
    )
    calibrated = build_portfolio_payload(
        repository,
        as_of=date(2026, 8, 4),
    )["products"][0]
    assert calibrated["latest_profit"] == "7.5"
    assert calibrated["holding_profit"] == "37.5"


def test_profit_calibration_sets_baseline_then_future_nav_profit_continues(
    tmp_path,
):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    projector = PositionProjector(repository)
    service = PortfolioTransactionService(repository, projector)
    repository.add_product(
        Product(
            "fund",
            "changsheng_fund",
            "003103",
            "公募基金",
            ProductType.PUBLIC_FUND,
        )
    )
    repository.create_transaction(
        Transaction(
            id="opening:fund",
            product_id="fund",
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 29),
            confirmation_date=date(2026, 7, 29),
            idempotency_key="opening:fund",
            amount=Decimal("100"),
            shares=Decimal("100"),
        )
    )
    for quote_date, nav in (
        (date(2026, 7, 29), "1"),
        (date(2026, 7, 30), "1.1"),
    ):
        repository.upsert_quote(
            "fund",
            MarketQuote(
                "003103",
                quote_date,
                "official",
                f"fund:{quote_date}",
                unit_nav=Decimal(nav),
            ),
            "2026-07-30T18:00:00+08:00",
        )
    projector.rebuild()

    service.adjust_holding_profit(
        "fund",
        Decimal("25"),
        date(2026, 7, 30),
        "平台累计收益校准",
        "web:profit-baseline",
    )
    calibrated_payload = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 30),
    )
    calibrated = calibrated_payload["products"][0]
    repository.upsert_quote(
        "fund",
        MarketQuote(
            "003103",
            date(2026, 7, 31),
            "official",
            "fund:2026-07-31",
            unit_nav=Decimal("1.2"),
        ),
        "2026-07-31T18:00:00+08:00",
    )
    advanced = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 31),
    )["products"][0]

    assert calibrated["holding_profit"] == "25"
    assert calibrated_payload["summary"]["latest_profit"] == "0"
    assert advanced["holding_profit"] == "35"


def test_sip_view_exposes_fee_rate_in_percent_units(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    repository.add_product(
        Product(
            "fund",
            "changsheng_fund",
            "015736",
            "基金D",
            ProductType.PUBLIC_FUND,
        )
    )
    repository.save_plan(
        SipPlan(
            id="plan",
            product_id="fund",
            daily_amount=Decimal("2000"),
            purchase_fee_rate=Decimal("0.00006"),
            source_cash_product_id="",
            status=SipPlanStatus.DRAFT,
            start_date=date(2026, 7, 30),
        )
    )

    plan = build_portfolio_payload(
        repository,
        as_of=date(2026, 7, 30),
    )["sip_plans"][0]

    assert plan["purchase_fee_rate"] == "0.00006"
    assert plan["purchase_fee_rate_percent"] == "0.006"


def test_sip_view_does_not_treat_non_trading_day_skip_as_last_execution(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.save_plan(
        SipPlan(
            id="plan-with-weekend-skip",
            product_id="fund",
            daily_amount=Decimal("20"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash",
            status=SipPlanStatus.ACTIVE,
            start_date=date(2026, 7, 30),
        )
    )
    repository.create_transaction(
        Transaction(
            id="sip-friday",
            product_id="fund",
            transaction_type=TransactionType.SIP_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 31),
            confirmation_date=date(2026, 7, 31),
            idempotency_key="sip-friday",
            amount=Decimal("20"),
            shares=Decimal("20"),
            plan_id="plan-with-weekend-skip",
        )
    )
    repository.save_plan_execution(
        PlanExecution(
            id="execution-friday",
            plan_id="plan-with-weekend-skip",
            intended_trade_date=date(2026, 7, 31),
            status="confirmed",
            transaction_id="sip-friday",
        )
    )
    repository.save_plan_execution(
        PlanExecution(
            id="execution-saturday",
            plan_id="plan-with-weekend-skip",
            intended_trade_date=date(2026, 8, 1),
            status="skipped",
            reason="non_trading_day",
        )
    )

    plan = build_portfolio_payload(
        repository,
        as_of=date(2026, 8, 1),
    )["sip_plans"][0]

    assert plan["last_execution"] == "2026-07-31"
    assert plan["skip_reason"] == ""


def test_sip_view_exposes_effective_cash_deduction_count_and_amount(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.save_plan(
        SipPlan(
            id="plan-with-deductions",
            product_id="fund",
            daily_amount=Decimal("20"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash",
            status=SipPlanStatus.ACTIVE,
            start_date=date(2026, 7, 30),
        )
    )
    for suffix, amount in (("one", "20"), ("two", "30")):
        repository.create_transaction(
            Transaction(
                id=f"cash-out-{suffix}",
                product_id="cash",
                transaction_type=TransactionType.CASH_TRANSFER_OUT,
                status=TransactionStatus.CONFIRMED,
                trade_date=date(2026, 7, 30),
                confirmation_date=date(2026, 7, 30),
                confirmation_nav=Decimal("1"),
                idempotency_key=f"cash-out-{suffix}",
                amount=Decimal(amount),
                shares=Decimal(amount),
                plan_id="plan-with-deductions",
            )
        )
    repository.create_transaction(
        Transaction(
            id="cash-out-refunded",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 31),
            confirmation_date=date(2026, 7, 31),
            confirmation_nav=Decimal("1"),
            idempotency_key="cash-out-refunded",
            amount=Decimal("40"),
            shares=Decimal("40"),
            plan_id="plan-with-deductions",
        )
    )
    repository.create_transaction(
        Transaction(
            id="cash-refund",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_IN,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 31),
            confirmation_date=date(2026, 7, 31),
            confirmation_nav=Decimal("1"),
            idempotency_key="cash-refund",
            amount=Decimal("40"),
            shares=Decimal("40"),
            linked_transaction_id="cash-out-refunded",
            plan_id="plan-with-deductions",
        )
    )

    plan = next(
        row for row in build_portfolio_payload(
            repository,
            as_of=date(2026, 8, 1),
        )["sip_plans"]
        if row["id"] == "plan-with-deductions"
    )

    assert plan["deduction_count"] == 2
    assert plan["deduction_amount"] == "50"


def test_sip_view_does_not_count_legacy_locked_cash_as_deducted(
    portfolio_fixture,
):
    repository = portfolio_fixture
    repository.save_plan(
        SipPlan(
            id="legacy-pending-plan",
            product_id="fund",
            daily_amount=Decimal("20"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash",
            status=SipPlanStatus.ACTIVE,
            start_date=date(2026, 7, 30),
        )
    )
    purchase = Transaction(
        id="legacy-pending-purchase",
        product_id="fund",
        transaction_type=TransactionType.SIP_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 7, 30),
        idempotency_key="legacy-pending-purchase",
        amount=Decimal("20"),
        plan_id="legacy-pending-plan",
    )
    cash = Transaction(
        id="legacy-pending-cash",
        product_id="cash",
        transaction_type=TransactionType.CASH_TRANSFER_OUT,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 7, 30),
        idempotency_key="legacy-pending-cash",
        amount=Decimal("20"),
        shares=Decimal("20"),
        linked_transaction_id=purchase.id,
        plan_id="legacy-pending-plan",
    )
    with repository.database.transaction() as conn:
        repository.create_transaction(purchase, conn)
        repository.create_transaction(cash, conn)
        repository.update_pending_transaction(
            replace(
                purchase,
                linked_transaction_id="legacy-pending-cash",
            ),
            conn,
        )
    repository.save_plan_execution(
        PlanExecution(
            id="legacy-pending-execution",
            plan_id="legacy-pending-plan",
            intended_trade_date=date(2026, 7, 30),
            status="pending_quote",
            transaction_id=purchase.id,
        )
    )

    plan = next(
        row
        for row in build_portfolio_payload(
            repository,
            as_of=date(2026, 8, 1),
        )["sip_plans"]
        if row["id"] == "legacy-pending-plan"
    )

    assert plan["deduction_count"] == 0
    assert plan["deduction_amount"] == "0"
