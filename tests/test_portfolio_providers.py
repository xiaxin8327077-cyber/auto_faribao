from datetime import date
from decimal import Decimal
import hashlib
import json
import urllib.parse

import pytest

from src.nav_monitor import ProviderError
from src.portfolio_models import MarketProduct, ProductType
from src.portfolio_providers import (
    CiticPortfolioProvider,
    NanyinPortfolioProvider,
    get_market_provider,
)


def test_nanyin_cash_fields_are_not_treated_as_nav():
    provider = NanyinPortfolioProvider(client=None)
    product = MarketProduct(
        "nanyin_wealth",
        "NYRR000007",
        "南银理财日日聚宝15号-A份额",
        ProductType.CASH_MANAGEMENT,
        "Z7003226000154",
    )
    quote = provider.quote_from_item(
        {
            "productCode": "NYRR000007",
            "date": "2026-07-30",
            "cumulativeNetValue": "0.4475",
            "netValue": "1.6315",
        },
        product,
    )
    assert quote.income_per_10k == Decimal("0.4475")
    assert quote.seven_day_annualized_rate == Decimal("0.016315")
    assert quote.unit_nav is None


def test_citic_cash_fields_ignore_fixed_nav():
    provider = CiticPortfolioProvider(client=None)
    product = MarketProduct(
        "citic_wealth",
        "AM264381F",
        "信银理财日盈象天天利618号-F",
        ProductType.CASH_MANAGEMENT,
        "Z7002626001878",
    )
    quote = provider.quote_from_item(
        {
            "prodCode": "AM264381F",
            "navDate": "2026-07-30",
            "nav": "1.0",
            "totalNav": "1.0",
            "tenThousandIncomeAmt": "0.4420",
            "outTenThousandIncomeAmt": "0.4400",
            "sevenDaysIncomeRate": "0.0162150",
        },
        product,
    )
    assert quote.income_per_10k == Decimal("0.4420")
    assert quote.seven_day_annualized_rate == Decimal("0.0162150")
    assert quote.unit_nav is None


def test_verified_cash_products_resolve_to_cash_management():
    assert (
        NanyinPortfolioProvider(client=None)
        .resolve_verified_identity("NYRR000007")
        .product_type
        is ProductType.CASH_MANAGEMENT
    )


def test_nanyin_wealth_nav_fields_preserve_legacy_meaning():
    provider = NanyinPortfolioProvider(client=None)
    product = MarketProduct(
        "nanyin_wealth",
        "NYZY000022",
        "南银理财致远一年定开19期A份额",
        ProductType.WEALTH_NAV,
    )
    item = {
        "productCode": "NYZY000022",
        "date": "2026-07-29",
        "netValue": "1.020000",
        "cumulativeNetValue": "1.031000",
    }

    quote = provider.quote_from_item(item, product)

    assert quote.unit_nav == Decimal("1.020000")
    assert quote.cumulative_nav == Decimal("1.031000")
    assert quote.income_per_10k is None
    assert quote.raw_hash == hashlib.sha256(
        json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_citic_wealth_nav_fields_preserve_legacy_meaning():
    provider = CiticPortfolioProvider(client=None)
    product = MarketProduct(
        "citic_wealth",
        "AF233276B",
        "慧盈象固收增强一年持有期5号B",
        ProductType.WEALTH_NAV,
    )

    quote = provider.quote_from_item(
        {
            "prodCode": "AF233276B",
            "navDateStr": "20260729",
            "navStr": "1.0780",
            "totalNavStr": "1.0910",
        },
        product,
    )

    assert quote.quote_date == date(2026, 7, 29)
    assert quote.unit_nav == Decimal("1.0780")
    assert quote.cumulative_nav == Decimal("1.0910")
    assert quote.income_per_10k is None


def test_citic_cash_uses_only_configured_income_field():
    provider = CiticPortfolioProvider(client=None)
    product = MarketProduct(
        "citic_wealth",
        "AM264381F",
        "信银理财日盈象天天利618号-F",
        ProductType.CASH_MANAGEMENT,
        metadata={"income_field": "outTenThousandIncomeAmt"},
    )

    quote = provider.quote_from_item(
        {
            "prodCode": "AM264381F",
            "navDate": "2026-07-30",
            "tenThousandIncomeAmt": "0.4420",
            "outTenThousandIncomeAmt": "0.4400",
            "sevenDaysIncomeRate": "0.0162150",
        },
        product,
    )

    assert quote.income_per_10k == Decimal("0.4400")


@pytest.mark.parametrize("income_field", ["nav", "totalNav", "unexpected"])
def test_citic_cash_rejects_unverified_income_field(income_field):
    provider = CiticPortfolioProvider(client=None)
    product = MarketProduct(
        "citic_wealth",
        "AM264381F",
        "信银理财日盈象天天利618号-F",
        ProductType.CASH_MANAGEMENT,
        metadata={"income_field": income_field},
    )

    with pytest.raises(ProviderError, match="^无法可靠识别产品类型$"):
        provider.quote_from_item(
            {
                "prodCode": "AM264381F",
                "navDate": "2026-07-30",
                "nav": "1.0",
                "totalNav": "1.0",
                "tenThousandIncomeAmt": "0.4420",
                "sevenDaysIncomeRate": "0.0162150",
            },
            product,
        )


@pytest.mark.parametrize(
    ("provider", "product"),
    [
        (
            CiticPortfolioProvider(client=None),
            MarketProduct(
                "citic_wealth",
                "003103",
                "not a Citic product",
                ProductType.PUBLIC_FUND,
            ),
        ),
        (
            NanyinPortfolioProvider(client=None),
            MarketProduct(
                "nanyin_wealth",
                "003103",
                "not a Nanyin product",
                ProductType.PUBLIC_FUND,
            ),
        ),
    ],
)
def test_bank_providers_reject_unsupported_product_types(provider, product):
    with pytest.raises(ProviderError, match="无法可靠识别产品类型"):
        provider.quote_from_item({}, product)


def test_verified_products_resolve_without_network():
    class FailOnNetwork:
        def __getattr__(self, _name):
            raise AssertionError("verified products must not require network")

    citic = CiticPortfolioProvider(client=FailOnNetwork())
    nanyin = NanyinPortfolioProvider(client=FailOnNetwork())

    assert citic.resolve_product(" am264381f ").registration_code == "Z7002626001878"
    assert nanyin.resolve_product(" nyrr000007 ").registration_code == "Z7003226000154"


def test_citic_classifies_new_cash_product_only_from_complete_quote_signature():
    class FakeCiticClient:
        def get_json(self, path, params):
            assert path.endswith("/getTAProductNav")
            assert params["prodCode"] == "AM999999A"
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {
                            "prodCode": "AM999999A",
                            "prodNameShort": "官方现金管理产品",
                            "registCode": "Z7002600000001",
                            "navDate": "2026-07-30",
                            "tenThousandIncomeAmt": "0.4100",
                            "sevenDaysIncomeRate": "0.0123",
                        }
                    ]
                },
            }

    product = CiticPortfolioProvider(FakeCiticClient()).resolve_product("am999999a")

    assert product == MarketProduct(
        "citic_wealth",
        "AM999999A",
        "官方现金管理产品",
        ProductType.CASH_MANAGEMENT,
        "Z7002600000001",
    )


def test_citic_rejects_unknown_product_with_only_fixed_nav_fields():
    class FakeCiticClient:
        def get_json(self, _path, _params):
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {
                            "prodCode": "AM999999A",
                            "prodNameShort": "ambiguous",
                            "navDate": "2026-07-30",
                            "nav": "1.0",
                            "totalNav": "1.0",
                        }
                    ]
                },
            }

    with pytest.raises(ProviderError, match="无法可靠识别产品类型"):
        CiticPortfolioProvider(FakeCiticClient()).resolve_product("AM999999A")


def test_citic_rejects_cash_signature_with_invalid_quote_values():
    class FakeCiticClient:
        def get_json(self, _path, _params):
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {
                            "prodCode": "AM999999A",
                            "prodNameShort": "invalid official quote",
                            "registCode": "Z7002600000001",
                            "navDate": "2026-07-30",
                            "tenThousandIncomeAmt": "not-a-number",
                            "sevenDaysIncomeRate": "0.0123",
                        }
                    ]
                },
            }

    with pytest.raises(ProviderError, match="无法可靠识别产品类型"):
        CiticPortfolioProvider(FakeCiticClient()).resolve_product("AM999999A")


@pytest.mark.parametrize(
    "annualized_rate",
    ["not-a-number", "NaN", "Infinity"],
)
def test_citic_rejects_unknown_cash_product_with_invalid_annualized_rate(
    annualized_rate,
):
    class FakeCiticClient:
        def get_json(self, _path, _params):
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {
                            "prodCode": "AM999999A",
                            "prodNameShort": "invalid official quote",
                            "registCode": "Z7002600000001",
                            "navDate": "2026-07-30",
                            "tenThousandIncomeAmt": "0.4100",
                            "sevenDaysIncomeRate": annualized_rate,
                        }
                    ]
                },
            }

    with pytest.raises(ProviderError, match="^无法可靠识别产品类型$"):
        CiticPortfolioProvider(FakeCiticClient()).resolve_product("AM999999A")


def test_nanyin_does_not_guess_unknown_type_from_numeric_values():
    class FakeNanyinClient:
        def post_encrypted_json(self, _path, _payload):
            raise AssertionError("no verified type rule exists for unknown products")

    with pytest.raises(ProviderError, match="无法可靠识别产品类型"):
        NanyinPortfolioProvider(FakeNanyinClient()).resolve_product("NYRR999999")


def test_citic_fetch_quotes_filters_range_and_sorts_newest_first():
    class FakeCiticClient:
        def get_json(self, path, params):
            assert path.endswith("/getTAProductNav")
            assert params["queryUnit"] == 1
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {
                            "prodCode": "AM264381F",
                            "navDate": "2026-07-28",
                            "tenThousandIncomeAmt": "0.4200",
                            "sevenDaysIncomeRate": "0.0160",
                        },
                        {
                            "prodCode": "AM264381F",
                            "navDate": "2026-07-30",
                            "tenThousandIncomeAmt": "0.4420",
                            "sevenDaysIncomeRate": "0.0162150",
                        },
                        {
                            "prodCode": "AM264381F",
                            "navDate": "2026-07-29",
                            "tenThousandIncomeAmt": "0.4300",
                            "sevenDaysIncomeRate": "0.0161",
                        },
                    ]
                },
            }

    provider = CiticPortfolioProvider(FakeCiticClient())
    product = provider.resolve_verified_identity("AM264381F")

    quotes = provider.fetch_quotes(
        product,
        date(2026, 7, 29),
        date(2026, 7, 30),
    )

    assert [quote.quote_date for quote in quotes] == [
        date(2026, 7, 30),
        date(2026, 7, 29),
    ]


def test_nanyin_fetch_quotes_filters_refs_range_and_sorts_newest_first():
    class FakeNanyinClient:
        def post_encrypted_json(self, path, payload):
            assert "queryNetValueList" in path
            assert payload == {
                "productCode": "NYRR000007",
                "startDate": "2026-07-29",
                "endDate": "2026-07-30",
                "currentPage": 1,
            }
            return {
                "aaData": [
                    {"$ref": "$.aaData[0]"},
                    {
                        "productCode": "NYRR000007",
                        "date": "2026-07-29",
                        "cumulativeNetValue": "0.4300",
                        "netValue": "1.6100",
                    },
                    {
                        "productCode": "NYRR000007",
                        "date": "2026-07-30",
                        "cumulativeNetValue": "0.4475",
                        "netValue": "1.6315",
                    },
                ]
            }

    provider = NanyinPortfolioProvider(FakeNanyinClient())
    product = provider.resolve_verified_identity("NYRR000007")

    quotes = provider.fetch_quotes(
        product,
        date(2026, 7, 29),
        date(2026, 7, 30),
    )

    assert [quote.quote_date for quote in quotes] == [
        date(2026, 7, 30),
        date(2026, 7, 29),
    ]


@pytest.mark.parametrize(
    ("provider", "product", "item"),
    [
        (
            CiticPortfolioProvider(client=None),
            MarketProduct(
                "citic_wealth",
                "EXPECTED",
                "expected",
                ProductType.WEALTH_NAV,
            ),
            {
                "prodCode": "OTHER",
                "navDate": "2026-07-30",
                "nav": "1.1",
            },
        ),
        (
            NanyinPortfolioProvider(client=None),
            MarketProduct(
                "nanyin_wealth",
                "EXPECTED",
                "expected",
                ProductType.WEALTH_NAV,
            ),
            {
                "productCode": "OTHER",
                "date": "2026-07-30",
                "netValue": "1.1",
            },
        ),
    ],
)
def test_quote_item_rejects_different_product_code(provider, product, item):
    with pytest.raises(ProviderError, match="产品代码不匹配"):
        provider.quote_from_item(item, product)


def test_market_provider_factory_returns_bank_adapters_and_rejects_unknown():
    assert isinstance(get_market_provider("citic_wealth"), CiticPortfolioProvider)
    assert isinstance(get_market_provider("nanyin_wealth"), NanyinPortfolioProvider)
    with pytest.raises(ProviderError, match="不支持的行情机构"):
        get_market_provider("unknown")
    assert (
        CiticPortfolioProvider(client=None)
        .resolve_verified_identity("AM264381F")
        .product_type
        is ProductType.CASH_MANAGEMENT
    )


def test_market_provider_factory_constructs_changsheng_seam(monkeypatch):
    import src.portfolio_providers as providers

    class PlaceholderChangshengProvider:
        pass

    monkeypatch.setattr(
        providers,
        "ChangshengFundProvider",
        PlaceholderChangshengProvider,
        raising=False,
    )

    assert isinstance(
        providers.get_market_provider("changsheng_fund"),
        PlaceholderChangshengProvider,
    )


def test_changsheng_pairs_date_and_nav_arrays_without_using_stale_date():
    from src.portfolio_providers import ChangshengFundProvider

    provider = ChangshengFundProvider(
        opener=lambda *_args, **_kwargs: {
            "status": 1,
            "DateArray": ["2026.07.28", "2026.07.29"],
            "DwjzArray": ["1.0319", "1.0321"],
        }
    )
    product = provider.resolve_product("003103")
    quotes = provider.fetch_quotes(
        product,
        date(2026, 7, 28),
        date(2026, 7, 30),
    )

    assert [(quote.quote_date, quote.unit_nav) for quote in quotes] == [
        (date(2026, 7, 29), Decimal("1.0321")),
        (date(2026, 7, 28), Decimal("1.0319")),
    ]
    assert not any(quote.quote_date == date(2026, 7, 30) for quote in quotes)


def test_changsheng_rejects_misaligned_arrays():
    from src.portfolio_providers import ChangshengFundProvider

    provider = ChangshengFundProvider(
        opener=lambda *_args, **_kwargs: {
            "status": 1,
            "DateArray": ["2026.07.29"],
            "DwjzArray": [],
        }
    )

    with pytest.raises(ProviderError, match="array lengths"):
        provider.fetch_quotes(
            provider.resolve_product("015736"),
            date(2026, 7, 29),
            date(2026, 7, 30),
        )


def test_changsheng_posts_exact_official_form():
    from src.portfolio_providers import ChangshengFundProvider

    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return {"status": 1, "DateArray": [], "DwjzArray": []}

    provider = ChangshengFundProvider(opener=opener)
    provider.fetch_quotes(
        provider.resolve_product("003103"),
        date(2026, 7, 28),
        date(2026, 7, 30),
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://www.csfunds.com.cn/front/ajax/invoke"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == (
        "application/x-www-form-urlencoded; charset=UTF-8"
    )
    form = urllib.parse.parse_qs(request.data.decode("utf-8"))
    assert form == {
        "_ZVING_METHOD": ["fund/loadNetWorth"],
        "_ZVING_URL": ["%2Fc%2F2022-05-10%2F190157.shtml"],
        "_ZVING_DATA": [
            '{"FundType":"Bond","FundCode":"003103",'
            '"TimeSlots":"2026-07-28~2026-07-30"}'
        ],
        "_ZVING_DATA_FORMAT": ["json"],
    }


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("003103", "长盛盛裕纯债债券型证券投资基金C类"),
        ("015736", "长盛盛裕纯债债券型证券投资基金D类"),
    ],
)
def test_changsheng_resolves_only_exact_fund_identities(code, name):
    from src.portfolio_providers import ChangshengFundProvider

    product = ChangshengFundProvider(
        opener=lambda *_args, **_kwargs: {}
    ).resolve_product(code)

    assert product == MarketProduct(
        "changsheng_fund",
        code,
        name,
        ProductType.PUBLIC_FUND,
    )


def test_changsheng_rejects_unknown_fund_code():
    from src.portfolio_providers import ChangshengFundProvider

    with pytest.raises(ProviderError, match="无法可靠识别产品类型"):
        ChangshengFundProvider(
            opener=lambda *_args, **_kwargs: {}
        ).resolve_product("003104")


@pytest.mark.parametrize(
    "response",
    [
        {"status": 0, "DateArray": [], "DwjzArray": []},
        {"status": 1, "DateArray": "2026.07.29", "DwjzArray": ["1.0321"]},
        {"status": 1, "DateArray": ["2026.07.29"], "DwjzArray": "1.0321"},
    ],
)
def test_changsheng_rejects_invalid_status_or_array_shapes(response):
    from src.portfolio_providers import ChangshengFundProvider

    provider = ChangshengFundProvider(
        opener=lambda *_args, **_kwargs: response
    )

    with pytest.raises(ProviderError):
        provider.fetch_quotes(
            provider.resolve_product("003103"),
            date(2026, 7, 29),
            date(2026, 7, 30),
        )


def test_changsheng_raw_hash_depends_only_on_each_date_nav_pair():
    from src.portfolio_providers import ChangshengFundProvider

    responses = iter(
        [
            {
                "status": 1,
                "DateArray": ["2026.07.29"],
                "DwjzArray": ["1.0321"],
                "ignored": "first response",
            },
            {
                "status": 1,
                "DateArray": ["2026.07.28", "2026.07.29"],
                "DwjzArray": ["1.0319", "1.0321"],
                "ignored": "second response",
            },
        ]
    )
    provider = ChangshengFundProvider(
        opener=lambda *_args, **_kwargs: next(responses)
    )
    product = provider.resolve_product("003103")

    first_hash = provider.fetch_quotes(
        product,
        date(2026, 7, 29),
        date(2026, 7, 29),
    )[0].raw_hash
    second_hash = provider.fetch_quotes(
        product,
        date(2026, 7, 28),
        date(2026, 7, 29),
    )[0].raw_hash

    assert first_hash == second_hash
