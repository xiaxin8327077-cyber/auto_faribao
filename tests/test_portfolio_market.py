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
