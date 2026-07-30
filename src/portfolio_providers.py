import hashlib
import json
from decimal import Decimal

from src.nav_monitor import (
    CiticHttpClient,
    NanyinHttpClient,
    ProviderError,
    _citic_query_unit,
    _first_list,
    _optional_decimal,
    _parse_decimal,
    _parse_nav_date,
    _unwrap_provider_response,
)
from src.portfolio_market import validate_market_quote
from src.portfolio_models import MarketProduct, MarketQuote, ProductType


VERIFIED_CASH_PRODUCTS = {
    ("nanyin_wealth", "NYRR000007"): (
        "南银理财日日聚宝15号-A份额",
        "Z7003226000154",
    ),
    ("citic_wealth", "AM264381F"): (
        "信银理财日盈象天天利618号-F",
        "Z7002626001878",
    ),
}


def _raw_hash(item: dict) -> str:
    payload = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if not normalized:
        raise ProviderError("产品代码为空")
    return normalized


def _require_product(provider: str, product: MarketProduct) -> None:
    if product.provider != provider:
        raise ProviderError("产品行情机构不匹配")


def _require_item_code(item_code, product: MarketProduct) -> str:
    actual = str(item_code or product.code).strip()
    if actual.upper() != product.code.upper():
        raise ProviderError("产品代码不匹配")
    return actual


class CiticPortfolioProvider:
    provider = "citic_wealth"
    NAV_PATH = "/cms.product/api/custom/productInfo/getTAProductNav"

    def __init__(self, client=None):
        self.client = client or CiticHttpClient()

    def resolve_verified_identity(self, code: str) -> MarketProduct:
        normalized = _normalize_code(code)
        identity = VERIFIED_CASH_PRODUCTS.get((self.provider, normalized))
        if identity is None:
            raise ProviderError("无法可靠识别产品类型")
        name, registration_code = identity
        return MarketProduct(
            self.provider,
            normalized,
            name,
            ProductType.CASH_MANAGEMENT,
            registration_code,
        )

    def resolve_product(self, code: str) -> MarketProduct:
        normalized = _normalize_code(code)
        identity = VERIFIED_CASH_PRODUCTS.get((self.provider, normalized))
        if identity is not None:
            return self.resolve_verified_identity(normalized)

        items = self._fetch_items(normalized, query_unit=1)
        exact_items = [
            item
            for item in items
            if str(item.get("prodCode") or "").strip().upper() == normalized
        ]
        if not exact_items:
            raise ProviderError("无法可靠识别产品类型")
        item = exact_items[0]
        income_field = next(
            (
                field
                for field in (
                    "tenThousandIncomeAmt",
                    "outTenThousandIncomeAmt",
                )
                if item.get(field) not in (None, "")
            ),
            "",
        )
        if not income_field or item.get("sevenDaysIncomeRate") in (None, ""):
            raise ProviderError("无法可靠识别产品类型")

        name = str(
            item.get("prodNameShort")
            or item.get("prodName")
            or item.get("name")
            or ""
        ).strip()
        registration_code = str(
            item.get("registCode") or item.get("registerCode") or ""
        ).strip()
        if not name or not registration_code:
            raise ProviderError("无法可靠识别产品类型")
        metadata = (
            {"income_field": income_field}
            if income_field != "tenThousandIncomeAmt"
            else None
        )
        product = MarketProduct(
            self.provider,
            normalized,
            name,
            ProductType.CASH_MANAGEMENT,
            registration_code,
            metadata,
        )
        try:
            validate_market_quote(
                product.product_type,
                self.quote_from_item(item, product),
            )
        except Exception as exc:
            raise ProviderError("无法可靠识别产品类型") from exc
        return product

    def fetch_quotes(self, product, start_date, end_date) -> list[MarketQuote]:
        _require_product(self.provider, product)
        query_unit = _citic_query_unit(start_date, today=end_date)
        quotes = [
            self.quote_from_item(item, product)
            for item in self._fetch_items(product.code, query_unit=query_unit)
            if isinstance(item, dict)
        ]
        return sorted(
            (
                quote
                for quote in quotes
                if start_date <= quote.quote_date <= end_date
            ),
            key=lambda quote: quote.quote_date,
            reverse=True,
        )

    def _fetch_items(self, code: str, query_unit: int) -> list:
        data = _unwrap_provider_response(
            self.client.get_json(
                self.NAV_PATH,
                {"prodCode": _normalize_code(code), "queryUnit": query_unit},
            )
        )
        return _first_list(data, "productNavPic", "list", "rows", "data")

    def quote_from_item(self, item: dict, product: MarketProduct) -> MarketQuote:
        _require_product(self.provider, product)
        product_code = _require_item_code(item.get("prodCode"), product)
        if product.product_type is ProductType.CASH_MANAGEMENT:
            income_field = str(
                (product.metadata or {}).get(
                    "income_field",
                    "tenThousandIncomeAmt",
                )
            )
            return MarketQuote(
                product_code=product_code,
                quote_date=_parse_nav_date(
                    item.get("navDate") or item.get("navDateStr")
                ),
                income_per_10k=_parse_decimal(item.get(income_field)),
                seven_day_annualized_rate=_optional_decimal(
                    item.get("sevenDaysIncomeRate")
                ),
                source=self.provider,
                raw_hash=_raw_hash(item),
            )
        if product.product_type is ProductType.WEALTH_NAV:
            return MarketQuote(
                product_code=product_code,
                quote_date=_parse_nav_date(
                    item.get("navDate") or item.get("navDateStr")
                ),
                unit_nav=_parse_decimal(item.get("nav") or item.get("navStr")),
                cumulative_nav=_optional_decimal(
                    item.get("totalNav") or item.get("totalNavStr")
                ),
                source=self.provider,
                raw_hash=_raw_hash(item),
            )
        raise ProviderError("无法可靠识别产品类型")


class NanyinPortfolioProvider:
    provider = "nanyin_wealth"
    NAV_PATH = "/eportal/ui?moduleId=5&portal.url=/portlet/article-data!queryNetValueList.portlet&TopModuelId=e7db4f2628cb40548f6101d71695ada2"

    def __init__(self, client=None):
        self.client = client or NanyinHttpClient()

    def resolve_verified_identity(self, code: str) -> MarketProduct:
        normalized = _normalize_code(code)
        identity = VERIFIED_CASH_PRODUCTS.get((self.provider, normalized))
        if identity is None:
            raise ProviderError("无法可靠识别产品类型")
        name, registration_code = identity
        return MarketProduct(
            self.provider,
            normalized,
            name,
            ProductType.CASH_MANAGEMENT,
            registration_code,
        )

    def resolve_product(self, code: str) -> MarketProduct:
        return self.resolve_verified_identity(code)

    def fetch_quotes(self, product, start_date, end_date) -> list[MarketQuote]:
        _require_product(self.provider, product)
        payload = {
            "productCode": product.code,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "currentPage": 1,
        }
        data = self.client.post_encrypted_json(self.NAV_PATH, payload)
        items = _first_list(data, "aaData", "result", "list", "rows", "data")
        quotes = [
            self.quote_from_item(item, product)
            for item in items
            if isinstance(item, dict) and "$ref" not in item
        ]
        return sorted(
            (
                quote
                for quote in quotes
                if start_date <= quote.quote_date <= end_date
            ),
            key=lambda quote: quote.quote_date,
            reverse=True,
        )

    def quote_from_item(self, item: dict, product: MarketProduct) -> MarketQuote:
        _require_product(self.provider, product)
        product_code = _require_item_code(item.get("productCode"), product)
        if product.product_type is ProductType.CASH_MANAGEMENT:
            income = _parse_decimal(item.get("cumulativeNetValue"))
            annualized_percent = _parse_decimal(item.get("netValue"))
            return MarketQuote(
                product_code=product_code,
                quote_date=_parse_nav_date(item.get("date") or item.get("navDate")),
                income_per_10k=income,
                seven_day_annualized_rate=annualized_percent / Decimal("100"),
                source=self.provider,
                raw_hash=_raw_hash(item),
            )
        if product.product_type is ProductType.WEALTH_NAV:
            return MarketQuote(
                product_code=product_code,
                quote_date=_parse_nav_date(item.get("date") or item.get("navDate")),
                unit_nav=_parse_decimal(
                    item.get("netValue") or item.get("netAssetValue")
                ),
                cumulative_nav=_optional_decimal(item.get("cumulativeNetValue")),
                source=self.provider,
                raw_hash=_raw_hash(item),
            )
        raise ProviderError("无法可靠识别产品类型")


def get_market_provider(provider: str):
    if provider == "citic_wealth":
        return CiticPortfolioProvider()
    if provider == "nanyin_wealth":
        return NanyinPortfolioProvider()
    if provider == "changsheng_fund":
        provider_class = globals().get("ChangshengFundProvider")
        if provider_class is None:
            raise ProviderError("长盛基金行情适配器尚未实现")
        return provider_class()
    raise ProviderError(f"不支持的行情机构：{provider}")
