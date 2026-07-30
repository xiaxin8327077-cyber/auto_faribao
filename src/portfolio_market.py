from datetime import date
from decimal import Decimal
from typing import Callable, Protocol

from src.beijing_time import now as beijing_now
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
    if any(value is not None and not isinstance(value, Decimal) for value in numeric):
        raise ValueError("quote decimals must be Decimal instances")
    if any(value is not None and not value.is_finite() for value in numeric):
        raise ValueError("quote decimals must be finite")

    if product_type in (ProductType.WEALTH_NAV, ProductType.PUBLIC_FUND):
        if quote.unit_nav is None or quote.unit_nav <= 0:
            raise ValueError("positive unit_nav is required")
    if product_type is ProductType.CASH_MANAGEMENT and quote.income_per_10k is None:
        raise ValueError("income_per_10k is required")


class QuoteSyncService:
    def __init__(self, repository, provider_factory: Callable[[str], MarketDataProvider]):
        self.repository = repository
        self.provider_factory = provider_factory

    def sync_product(
        self, product: MarketProduct, start_date: date, end_date: date
    ) -> list[MarketQuote]:
        provider = self.provider_factory(product.provider)
        quotes = provider.fetch_quotes(product, start_date, end_date)
        for quote in quotes:
            if quote.product_code.upper() != product.code.upper():
                raise ValueError("provider returned a different product code")
            validate_market_quote(product.product_type, quote)

        persisted_product = self.repository.get_product_by_provider_code(
            product.provider, product.code
        )
        if persisted_product is None:
            raise ValueError("product is not registered")

        fetched_at = beijing_now().isoformat(timespec="seconds")
        for quote in quotes:
            self.repository.upsert_quote(persisted_product.id, quote, fetched_at)
        return sorted(quotes, key=lambda item: item.quote_date, reverse=True)
