from datetime import date
from typing import Protocol

from src.portfolio_models import MarketProduct, MarketQuote, ProductType


class MarketDataProvider(Protocol):
    provider: str

    def resolve_product(self, code: str) -> MarketProduct: ...

    def fetch_quotes(
        self, product: MarketProduct, start_date: date, end_date: date
    ) -> list[MarketQuote]: ...


def validate_market_quote(product_type: ProductType, quote: MarketQuote) -> None:
    if not quote.product_code.strip() or not quote.source.strip() or not quote.raw_hash:
        raise ValueError("quote identity fields are required")

    numeric = (
        quote.unit_nav,
        quote.cumulative_nav,
        quote.income_per_10k,
        quote.seven_day_annualized_rate,
    )
    if any(value is not None and not value.is_finite() for value in numeric):
        raise ValueError("quote decimals must be finite")

    if product_type in (ProductType.WEALTH_NAV, ProductType.PUBLIC_FUND):
        if quote.unit_nav is None or quote.unit_nav <= 0:
            raise ValueError("positive unit_nav is required")
    if product_type is ProductType.CASH_MANAGEMENT and quote.income_per_10k is None:
        raise ValueError("income_per_10k is required")
