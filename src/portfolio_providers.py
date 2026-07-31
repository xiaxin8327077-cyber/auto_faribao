import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime
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
CITIC_CASH_INCOME_FIELDS = frozenset(
    ("tenThousandIncomeAmt", "outTenThousandIncomeAmt")
)
FUND_IDENTITIES = {
    "003103": ("长盛盛裕纯债债券型证券投资基金C类", "Bond", "003103"),
    "015736": ("长盛盛裕纯债债券型证券投资基金D类", "Bond", "015736"),
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


def _citic_cash_income_field(product: MarketProduct) -> str:
    income_field = str(
        (product.metadata or {}).get(
            "income_field",
            "tenThousandIncomeAmt",
        )
    )
    if income_field not in CITIC_CASH_INCOME_FIELDS:
        raise ProviderError("无法可靠识别产品类型")
    return income_field


def _open_json_request(request, timeout=10) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    try:
        return json.loads(raw.decode(charset, "replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"接口返回不是有效 JSON：{exc}") from exc


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
        try:
            annualized_rate = _parse_decimal(item.get("sevenDaysIncomeRate"))
        except Exception as exc:
            raise ProviderError("无法可靠识别产品类型") from exc
        if not annualized_rate.is_finite():
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
            income_field = _citic_cash_income_field(product)
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


class ChangshengFundProvider:
    provider = "changsheng_fund"
    URL = "https://www.csfunds.com.cn/front/ajax/invoke"

    def __init__(self, opener=None):
        self.opener = opener or _open_json_request

    def resolve_product(self, code: str) -> MarketProduct:
        normalized = _normalize_code(code)
        identity = FUND_IDENTITIES.get(normalized)
        if identity is None:
            raise ProviderError("无法可靠识别产品类型")
        name, _fund_type, registration_code = identity
        return MarketProduct(
            self.provider,
            normalized,
            name,
            ProductType.PUBLIC_FUND,
            registration_code,
        )

    def fetch_quotes(self, product, start_date, end_date) -> list[MarketQuote]:
        _require_product(self.provider, product)
        resolved_product = self.resolve_product(product.code)
        _name, fund_type = FUND_IDENTITIES[resolved_product.code][:2]
        form = {
            "_ZVING_METHOD": "fund/loadNetWorth",
            "_ZVING_URL": "%2Fc%2F2022-05-10%2F190157.shtml",
            "_ZVING_DATA": json.dumps(
                {
                    "FundType": fund_type,
                    "FundCode": resolved_product.code,
                    "TimeSlots": (
                        f"{start_date.isoformat()}~{end_date.isoformat()}"
                    ),
                },
                separators=(",", ":"),
            ),
            "_ZVING_DATA_FORMAT": "json",
        }
        request = urllib.request.Request(
            self.URL,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded; charset=UTF-8"
                ),
            },
            method="POST",
        )
        try:
            data = self.opener(request, timeout=10)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("长盛基金行情接口请求失败") from exc
        status = data.get("status") if isinstance(data, dict) else None
        if not (type(status) is int and status == 1):
            raise ProviderError("长盛基金行情接口状态异常")

        date_values = data.get("DateArray")
        nav_values = data.get("DwjzArray")
        if not isinstance(date_values, list) or not isinstance(nav_values, list):
            raise ProviderError("Changsheng date/NAV arrays must be lists")
        if len(date_values) != len(nav_values):
            raise ProviderError("Changsheng date/NAV array lengths differ")

        quotes = []
        for raw_date, raw_nav in zip(date_values, nav_values):
            try:
                quote_date = datetime.strptime(
                    str(raw_date).strip(),
                    "%Y.%m.%d",
                ).date()
                unit_nav = _parse_decimal(raw_nav)
                quote = MarketQuote(
                    product_code=resolved_product.code,
                    quote_date=quote_date,
                    unit_nav=unit_nav,
                    source=self.provider,
                    raw_hash=_raw_hash(
                        {
                            "quote_date": str(raw_date).strip(),
                            "unit_nav": str(raw_nav).strip(),
                        }
                    ),
                )
                validate_market_quote(resolved_product.product_type, quote)
            except Exception as exc:
                raise ProviderError("长盛基金净值数据无效") from exc
            if start_date <= quote_date <= end_date:
                quotes.append(quote)
        return sorted(
            quotes,
            key=lambda quote: quote.quote_date,
            reverse=True,
        )


def get_market_provider(provider: str):
    if provider == "citic_wealth":
        return CiticPortfolioProvider()
    if provider == "nanyin_wealth":
        return NanyinPortfolioProvider()
    if provider == "changsheng_fund":
        return ChangshengFundProvider()
    raise ProviderError(f"不支持的行情机构：{provider}")
