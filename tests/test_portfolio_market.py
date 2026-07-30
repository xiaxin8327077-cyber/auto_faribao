from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_market import MarketQuote, validate_market_quote
from src.portfolio_models import MarketProduct, ProductType


def test_cash_quote_requires_per_10k_and_allows_negative_income():
    quote = MarketQuote(
        product_code="NYRR000007",
        quote_date=date(2026, 7, 30),
        income_per_10k=Decimal("-0.0100"),
        seven_day_annualized_rate=Decimal("0.016315"),
        source="nanyin_wealth",
        raw_hash="abc",
    )

    validate_market_quote(ProductType.CASH_MANAGEMENT, quote)


def test_cash_quote_rejects_fixed_nav_without_per_10k():
    quote = MarketQuote(
        product_code="AM264381F",
        quote_date=date(2026, 7, 30),
        unit_nav=Decimal("1"),
        source="citic_wealth",
        raw_hash="abc",
    )

    with pytest.raises(ValueError, match="income_per_10k"):
        validate_market_quote(ProductType.CASH_MANAGEMENT, quote)


def test_public_fund_quote_requires_positive_unit_nav():
    quote = MarketQuote(
        product_code="003103",
        quote_date=date(2026, 7, 30),
        unit_nav=Decimal("0"),
        source="changsheng_fund",
        raw_hash="abc",
    )

    with pytest.raises(ValueError, match="positive unit_nav"):
        validate_market_quote(ProductType.PUBLIC_FUND, quote)


@pytest.mark.parametrize("unit_nav", [1, 1.0, "1"])
def test_quote_rejects_non_decimal_numeric_values(unit_nav):
    quote = MarketQuote(
        product_code="003103",
        quote_date=date(2026, 7, 30),
        unit_nav=unit_nav,
        source="changsheng_fund",
        raw_hash="abc",
    )

    with pytest.raises(ValueError, match="Decimal"):
        validate_market_quote(ProductType.PUBLIC_FUND, quote)


@pytest.mark.parametrize("unit_nav", [Decimal("NaN"), Decimal("Infinity")])
def test_quote_rejects_non_finite_decimal_values(unit_nav):
    quote = MarketQuote(
        product_code="003103",
        quote_date=date(2026, 7, 30),
        unit_nav=unit_nav,
        source="changsheng_fund",
        raw_hash="abc",
    )

    with pytest.raises(ValueError, match="finite"):
        validate_market_quote(ProductType.PUBLIC_FUND, quote)


def test_market_product_metadata_is_an_immutable_snapshot():
    source_metadata = {"region": "CN"}
    product = MarketProduct(
        provider="fund",
        code="003103",
        name="长盛盛裕纯债C",
        product_type=ProductType.PUBLIC_FUND,
        metadata=source_metadata,
    )

    source_metadata["region"] = "US"

    assert product.metadata == {"region": "CN"}
    with pytest.raises(TypeError):
        product.metadata["region"] = "US"


def test_quote_sync_fetches_outside_transaction_and_upserts_once(tmp_path):
    from src.portfolio_db import PortfolioDatabase
    from src.portfolio_models import Product, ProductStatus
    from src.portfolio_repository import PortfolioRepository
    from src.portfolio_market import QuoteSyncService

    class Provider:
        provider = "changsheng_fund"
        calls = 0

        def fetch_quotes(self, product, start_date, end_date):
            self.calls += 1
            return [MarketQuote(
                product_code=product.code,
                quote_date=date(2026, 7, 29),
                unit_nav=Decimal("1.0321"),
                source=self.provider,
                raw_hash="same",
            )]

    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    repo = PortfolioRepository(db)
    repo.add_product(Product(
        "p1", "changsheng_fund", "003103", "长盛盛裕纯债C",
        ProductType.PUBLIC_FUND, ProductStatus.ACTIVE,
    ))
    provider = Provider()
    service = QuoteSyncService(repo, lambda _name: provider)
    market_product = MarketProduct(
        "changsheng_fund", "003103", "长盛盛裕纯债C",
        ProductType.PUBLIC_FUND,
    )

    service.sync_product(market_product, date(2026, 7, 29), date(2026, 7, 29))
    service.sync_product(market_product, date(2026, 7, 29), date(2026, 7, 29))

    assert provider.calls == 2
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1


def test_quote_repository_round_trips_and_returns_latest_quote(tmp_path):
    from src.portfolio_db import PortfolioDatabase
    from src.portfolio_models import Product
    from src.portfolio_repository import PortfolioRepository

    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    repo = PortfolioRepository(db)
    repo.add_product(Product(
        "p1", "changsheng_fund", "003103", "长盛盛裕纯债C",
        ProductType.PUBLIC_FUND,
    ))
    first = MarketQuote(
        product_code="003103",
        quote_date=date(2026, 7, 28),
        unit_nav=Decimal("1.0310"),
        source="changsheng_fund",
        raw_hash="first",
    )
    second = MarketQuote(
        product_code="003103",
        quote_date=date(2026, 7, 29),
        unit_nav=Decimal("1.0321"),
        source="changsheng_fund",
        raw_hash="second",
    )

    assert repo.upsert_quote("p1", first, "2026-07-30T09:00:00+08:00") == first
    assert repo.upsert_quote("p1", second, "2026-07-30T09:01:00+08:00") == second

    assert repo.get_quote("p1", date(2026, 7, 28)) == first
    assert repo.get_quote("p1", date(2026, 7, 27)) is None
    assert repo.latest_quote("p1") == second
    assert repo.latest_quote("p1", date(2026, 7, 28)) == first
