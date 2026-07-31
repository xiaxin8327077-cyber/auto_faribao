from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
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
from src.portfolio_view import build_portfolio_payload


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

    profit_on_sip_confirmation = build_portfolio_payload(
        repository, as_of=date(2026, 7, 31)
    )["products"][0]["latest_profit"]
    profit_on_redemption_confirmation = build_portfolio_payload(
        repository, as_of=date(2026, 8, 3)
    )["products"][0]["latest_profit"]
    profit_after_redemption = build_portfolio_payload(
        repository, as_of=date(2026, 8, 4)
    )["products"][0]["latest_profit"]

    assert profit_on_sip_confirmation == "10"
    assert profit_on_redemption_confirmation == "15"
    assert profit_after_redemption == "13"
