# Portfolio Ledger Phase 2: Market Data and Product Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce type-aware market quotes, correct the cash-management field mappings for 南银/信银, add the 长盛 public-fund adapter, and persist official quotes idempotently.

**Architecture:** A provider-neutral `MarketQuote` contract separates product identity from source-specific JSON. Provider adapters are responsible for both product-type recognition and field semantics. `QuoteSyncService` fetches outside a database transaction, validates the normalized records, then upserts them through the Phase 1 repository.

**Tech Stack:** Python 3.10+, standard-library `urllib`, existing Citic/Nanyin HTTP clients, `Decimal`, `hashlib`, SQLite repository, `pytest`.

## Global Constraints

- Prerequisite: Phase 1 commit and tests are green.
- Supported product types are exactly `wealth_nav`, `cash_management`, and `public_fund`.
- Cash-management income is calculated from `income_per_10k`; seven-day annualized yield is display-only.
- Never interpret fixed `nav=1.0` as cash-management income.
- Never use an older fund NAV to confirm a target trade date.
- Every provider must validate code, date, finite decimals, and fields required by the identified product type.
- `NYRR000007` maps 南银 `cumulativeNetValue` to per-10k income and `netValue` to the displayed annualized percent.
- `AM264381F` maps 信银 `tenThousandIncomeAmt` to per-10k income and `sevenDaysIncomeRate` to a decimal annualized rate.
- 长盛 codes `003103` and `015736` are independent products even though they share one master fund.
- Do not stage runtime databases, source credentials, tokens, JSON state, logs, screenshots, or unrelated changes.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/portfolio_market.py` | Normalized quote/identity records, validation, sync service, provider protocol |
| `src/portfolio_providers.py` | Citic, Nanyin, and Changsheng provider implementations and registry |
| `src/portfolio_repository.py` | Quote upsert and product lookup methods |
| `src/nav_monitor.py` | Compatibility conversion for existing reports during staged rollout |
| `tests/test_portfolio_market.py` | Type validation, idempotent sync, stale-date behavior |
| `tests/test_portfolio_providers.py` | Source-field fixture parsing and official request shape |
| `tests/test_nav_monitor.py` | Existing report compatibility regression |

### Task 1: Define normalized product and quote contracts

**Files:**
- Modify: `src/portfolio_models.py`
- Create: `src/portfolio_market.py`
- Create: `tests/test_portfolio_market.py`

**Interfaces:**
- Produces: `MarketProduct(provider, code, name, product_type, registration_code="", metadata=None)`.
- Produces: `MarketQuote(product_code, quote_date, source, raw_hash, unit_nav=None, cumulative_nav=None, income_per_10k=None, seven_day_annualized_rate=None, source_timestamp="")`.
- Produces: `MarketDataProvider.resolve_product(code) -> MarketProduct`.
- Produces: `MarketDataProvider.fetch_quotes(product, start_date, end_date) -> list[MarketQuote]`.
- Produces: `validate_market_quote(product_type, quote) -> None`.

- [ ] **Step 1: Write failing validation tests**

```python
# tests/test_portfolio_market.py
from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_market import MarketQuote, validate_market_quote
from src.portfolio_models import ProductType


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
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_market.py -q`

Expected: FAIL with missing `src.portfolio_market`.

- [ ] **Step 3: Add the normalized records and exact validation**

Append to `src/portfolio_models.py`:

```python
from typing import Mapping


@dataclass(frozen=True)
class MarketProduct:
    provider: str
    code: str
    name: str
    product_type: ProductType
    registration_code: str = ""
    metadata: Mapping[str, str] = None


@dataclass(frozen=True)
class MarketQuote:
    product_code: str
    quote_date: date
    source: str
    raw_hash: str
    unit_nav: Optional[Decimal] = None
    cumulative_nav: Optional[Decimal] = None
    income_per_10k: Optional[Decimal] = None
    seven_day_annualized_rate: Optional[Decimal] = None
    source_timestamp: str = ""
```

Re-export the two dataclasses from `src/portfolio_market.py`, define the provider protocol, and implement:

```python
from typing import Protocol

from src.portfolio_models import MarketProduct, MarketQuote, ProductType


class MarketDataProvider(Protocol):
    provider: str

    def resolve_product(self, code: str) -> MarketProduct: ...

    def fetch_quotes(
        self, product: MarketProduct, start_date, end_date
    ) -> list[MarketQuote]: ...


def validate_market_quote(product_type: ProductType, quote: MarketQuote) -> None:
    if not quote.product_code.strip() or not quote.source.strip() or not quote.raw_hash:
        raise ValueError("quote identity fields are required")
    numeric = (
        quote.unit_nav, quote.cumulative_nav,
        quote.income_per_10k, quote.seven_day_annualized_rate,
    )
    if any(value is not None and not value.is_finite() for value in numeric):
        raise ValueError("quote decimals must be finite")
    if product_type in (ProductType.WEALTH_NAV, ProductType.PUBLIC_FUND):
        if quote.unit_nav is None or quote.unit_nav <= 0:
            raise ValueError("positive unit_nav is required")
    if product_type is ProductType.CASH_MANAGEMENT:
        if quote.income_per_10k is None:
            raise ValueError("income_per_10k is required")
```

- [ ] **Step 4: Run validation tests**

Run: `pytest tests/test_portfolio_market.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit the contract**

```bash
git add src/portfolio_models.py src/portfolio_market.py tests/test_portfolio_market.py
git commit -m "feat: add type-aware market quote contract"
```

### Task 2: Add quote persistence and idempotent sync

**Files:**
- Modify: `src/portfolio_repository.py`
- Modify: `src/portfolio_market.py`
- Modify: `tests/test_portfolio_market.py`

**Interfaces:**
- Produces: `PortfolioRepository.upsert_quote(product_id, quote, fetched_at) -> MarketQuote`.
- Produces: `PortfolioRepository.get_quote(product_id, quote_date) -> MarketQuote | None`.
- Produces: `PortfolioRepository.latest_quote(product_id, on_or_before=None) -> MarketQuote | None`.
- Produces: `QuoteSyncService.sync_product(product, start_date, end_date) -> list[MarketQuote]`.

- [ ] **Step 1: Write a failing duplicate-sync test**

```python
def test_quote_sync_fetches_outside_transaction_and_upserts_once(tmp_path):
    from src.portfolio_db import PortfolioDatabase
    from src.portfolio_models import Product, ProductStatus
    from src.portfolio_repository import PortfolioRepository
    from src.portfolio_market import MarketProduct, QuoteSyncService

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

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
```

- [ ] **Step 2: Verify the new test fails**

Run: `pytest tests/test_portfolio_market.py::test_quote_sync_fetches_outside_transaction_and_upserts_once -q`

Expected: FAIL because `QuoteSyncService` or quote repository methods do not exist.

- [ ] **Step 3: Implement quote upsert and sync**

The upsert must use:

```sql
INSERT INTO quotes (
    id, product_id, quote_date, unit_nav, cumulative_nav,
    income_per_10k, seven_day_annualized_rate, source,
    source_timestamp, raw_hash, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(product_id, quote_date) DO UPDATE SET
    unit_nav = excluded.unit_nav,
    cumulative_nav = excluded.cumulative_nav,
    income_per_10k = excluded.income_per_10k,
    seven_day_annualized_rate = excluded.seven_day_annualized_rate,
    source = excluded.source,
    source_timestamp = excluded.source_timestamp,
    raw_hash = excluded.raw_hash,
    fetched_at = excluded.fetched_at
```

Use deterministic quote ID `uuid5(NAMESPACE_URL, f"{product_id}:{quote.quote_date.isoformat()}")`.

Implement `QuoteSyncService.sync_product` as:

```python
def sync_product(self, product, start_date, end_date):
    provider = self.provider_factory(product.provider)
    quotes = provider.fetch_quotes(product, start_date, end_date)
    for quote in quotes:
        if quote.product_code.upper() != product.code.upper():
            raise ValueError("provider returned a different product code")
        validate_market_quote(product.product_type, quote)
    fetched_at = beijing_now().isoformat(timespec="seconds")
    for quote in quotes:
        self.repository.upsert_quote(
            self.repository.get_product_by_provider_code(
                product.provider, product.code
            ).id,
            quote,
            fetched_at,
        )
    return sorted(quotes, key=lambda item: item.quote_date, reverse=True)
```

- [ ] **Step 4: Run market and repository tests**

Run: `pytest tests/test_portfolio_market.py tests/test_portfolio_repository.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit quote sync**

```bash
git add src/portfolio_repository.py src/portfolio_market.py tests/test_portfolio_market.py
git commit -m "feat: persist normalized market quotes"
```

### Task 3: Correct Citic and Nanyin cash-management parsing

**Files:**
- Create: `src/portfolio_providers.py`
- Create: `tests/test_portfolio_providers.py`
- Modify: `src/nav_monitor.py:1602-1869`

**Interfaces:**
- Consumes: existing `CiticHttpClient`, `NanyinHttpClient`, `_unwrap_provider_response`, `_first_list`, `_parse_nav_date`.
- Produces: `CiticPortfolioProvider`, `NanyinPortfolioProvider`.
- Produces: `get_market_provider("citic_wealth" | "nanyin_wealth" | "changsheng_fund")`.

- [ ] **Step 1: Write exact source-fixture parsing tests**

```python
# tests/test_portfolio_providers.py
from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_models import MarketProduct, ProductType
from src.portfolio_providers import CiticPortfolioProvider, NanyinPortfolioProvider


def test_nanyin_cash_fields_are_not_treated_as_nav():
    provider = NanyinPortfolioProvider(client=None)
    product = MarketProduct(
        "nanyin_wealth", "NYRR000007", "南银理财日日聚宝15号-A份额",
        ProductType.CASH_MANAGEMENT, "Z7003226000154",
    )
    quote = provider.quote_from_item({
        "productCode": "NYRR000007",
        "date": "2026-07-30",
        "cumulativeNetValue": "0.4475",
        "netValue": "1.6315",
    }, product)
    assert quote.income_per_10k == Decimal("0.4475")
    assert quote.seven_day_annualized_rate == Decimal("0.016315")
    assert quote.unit_nav is None


def test_citic_cash_fields_ignore_fixed_nav():
    provider = CiticPortfolioProvider(client=None)
    product = MarketProduct(
        "citic_wealth", "AM264381F", "信银理财日盈象天天利618号-F",
        ProductType.CASH_MANAGEMENT, "Z7002626001878",
    )
    quote = provider.quote_from_item({
        "prodCode": "AM264381F",
        "navDate": "2026-07-30",
        "nav": "1.0",
        "totalNav": "1.0",
        "tenThousandIncomeAmt": "0.4420",
        "outTenThousandIncomeAmt": "0.4400",
        "sevenDaysIncomeRate": "0.0162150",
    }, product)
    assert quote.income_per_10k == Decimal("0.4420")
    assert quote.seven_day_annualized_rate == Decimal("0.0162150")
    assert quote.unit_nav is None


def test_verified_cash_products_resolve_to_cash_management():
    assert NanyinPortfolioProvider(client=None).resolve_verified_identity(
        "NYRR000007"
    ).product_type is ProductType.CASH_MANAGEMENT
    assert CiticPortfolioProvider(client=None).resolve_verified_identity(
        "AM264381F"
    ).product_type is ProductType.CASH_MANAGEMENT
```

- [ ] **Step 2: Verify fixture tests fail**

Run: `pytest tests/test_portfolio_providers.py -q`

Expected: FAIL because `src.portfolio_providers` does not exist.

- [ ] **Step 3: Implement type-aware provider parsing**

Implement `quote_from_item` with explicit product-type branches. The Nanyin cash branch is:

```python
if product.product_type is ProductType.CASH_MANAGEMENT:
    income = _parse_decimal(item.get("cumulativeNetValue"))
    annualized_percent = _parse_decimal(item.get("netValue"))
    return MarketQuote(
        product_code=str(item.get("productCode") or product.code),
        quote_date=_parse_nav_date(item.get("date") or item.get("navDate")),
        income_per_10k=income,
        seven_day_annualized_rate=annualized_percent / Decimal("100"),
        source=self.provider,
        raw_hash=_raw_hash(item),
    )
```

The Citic cash branch is:

```python
if product.product_type is ProductType.CASH_MANAGEMENT:
    income_field = str((product.metadata or {}).get(
        "income_field", "tenThousandIncomeAmt"
    ))
    return MarketQuote(
        product_code=str(item.get("prodCode") or product.code),
        quote_date=_parse_nav_date(item.get("navDate") or item.get("navDateStr")),
        income_per_10k=_parse_decimal(item.get(income_field)),
        seven_day_annualized_rate=_optional_decimal(item.get("sevenDaysIncomeRate")),
        source=self.provider,
        raw_hash=_raw_hash(item),
    )
```

For `wealth_nav`, preserve the existing `nav`/`totalNav` and `netValue`/`cumulativeNetValue` behavior. `_raw_hash` is SHA-256 of `json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`.

Define a verified identity registry for the first cash products:

```python
VERIFIED_CASH_PRODUCTS = {
    ("nanyin_wealth", "NYRR000007"): (
        "南银理财日日聚宝15号-A份额", "Z7003226000154"
    ),
    ("citic_wealth", "AM264381F"): (
        "信银理财日盈象天天利618号-F", "Z7002626001878"
    ),
}
```

Citic may additionally classify a new product as cash management only when its official quote payload contains both a per-10k field and `sevenDaysIncomeRate`. Nanyin products outside the verified registry require an explicit, tested official product-type field before classification. If identity or type remains ambiguous, raise `ProviderError("无法可靠识别产品类型")`; never infer type from a numeric range or silently default an ambiguous web-added product to `wealth_nav`.

Do not change the existing legacy `NavRecord` behavior in this task; add comments at the old provider methods pointing new portfolio code to the type-aware adapters.

- [ ] **Step 4: Run provider plus legacy provider tests**

Run: `pytest tests/test_portfolio_providers.py tests/test_nav_monitor.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit cash adapters**

```bash
git add src/portfolio_providers.py tests/test_portfolio_providers.py src/nav_monitor.py
git commit -m "fix: parse cash management market fields"
```

### Task 4: Add the Changsheng official fund provider

**Files:**
- Modify: `src/portfolio_providers.py`
- Modify: `tests/test_portfolio_providers.py`

**Interfaces:**
- Produces: `ChangshengFundProvider`.
- `resolve_product("003103" | "015736") -> MarketProduct`.
- `fetch_quotes(product, start_date, end_date) -> list[MarketQuote]`.
- Official POST URL: `https://www.csfunds.com.cn/front/ajax/invoke`.

- [ ] **Step 1: Write request-shape and date-pairing tests**

```python
def test_changsheng_pairs_date_and_nav_arrays_without_using_stale_date():
    from src.portfolio_providers import ChangshengFundProvider

    provider = ChangshengFundProvider(opener=lambda *_args, **_kwargs: {
        "status": 1,
        "DateArray": ["2026.07.28", "2026.07.29"],
        "DwjzArray": ["1.0319", "1.0321"],
    })
    product = provider.resolve_product("003103")
    quotes = provider.fetch_quotes(
        product, date(2026, 7, 28), date(2026, 7, 30)
    )
    assert [(q.quote_date, q.unit_nav) for q in quotes] == [
        (date(2026, 7, 29), Decimal("1.0321")),
        (date(2026, 7, 28), Decimal("1.0319")),
    ]
    assert not any(q.quote_date == date(2026, 7, 30) for q in quotes)


def test_changsheng_rejects_misaligned_arrays():
    from src.portfolio_providers import ChangshengFundProvider, ProviderError

    provider = ChangshengFundProvider(opener=lambda *_args, **_kwargs: {
        "status": 1,
        "DateArray": ["2026.07.29"],
        "DwjzArray": [],
    })
    with pytest.raises(ProviderError, match="array lengths"):
        provider.fetch_quotes(
            provider.resolve_product("015736"),
            date(2026, 7, 29), date(2026, 7, 30),
        )
```

- [ ] **Step 2: Verify the new tests fail**

Run: `pytest tests/test_portfolio_providers.py -q`

Expected: FAIL because `ChangshengFundProvider` is missing.

- [ ] **Step 3: Implement the official POST adapter**

Use form fields:

```python
{
    "_ZVING_METHOD": "fund/loadNetWorth",
    "_ZVING_URL": "%2Fc%2F2022-05-10%2F190157.shtml",
    "_ZVING_DATA": json.dumps({
        "FundType": "Bond",
        "FundCode": product.code,
        "TimeSlots": f"{start_date.isoformat()}~{end_date.isoformat()}",
    }, separators=(",", ":")),
    "_ZVING_DATA_FORMAT": "json",
}
```

Define exact identities:

```python
FUND_IDENTITIES = {
    "003103": ("长盛盛裕纯债债券型证券投资基金C类", "Bond"),
    "015736": ("长盛盛裕纯债债券型证券投资基金D类", "Bond"),
}
```

Validate `status == 1`, equal array lengths, parse `%Y.%m.%d`, filter to the requested inclusive range, calculate a raw hash from the paired date/NAV values, and sort newest first. `resolve_product` rejects every other code.

- [ ] **Step 4: Run provider tests**

Run: `pytest tests/test_portfolio_providers.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the fund adapter**

```bash
git add src/portfolio_providers.py tests/test_portfolio_providers.py
git commit -m "feat: add changsheng fund market provider"
```

### Task 5: Add compatibility conversion for existing reports

**Files:**
- Modify: `src/nav_monitor.py:119-176`
- Modify: `src/nav_monitor.py:588-966`
- Modify: `tests/test_nav_monitor.py`

**Interfaces:**
- Produces: `market_quote_to_nav_record(product, quote) -> NavRecord`.
- Conversion is allowed only for `wealth_nav` and `public_fund`.
- Cash-management quotes must raise `ValueError("cash management quote has no unit NAV")`; Phase 4 adds their dedicated report rows.

- [ ] **Step 1: Write compatibility tests**

```python
def test_public_fund_market_quote_converts_to_legacy_nav_record():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    product = MarketProduct(
        "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND
    )
    quote = MarketQuote(
        "003103", date(2026, 7, 29), "changsheng_fund", "hash",
        unit_nav=Decimal("1.0321"),
    )
    record = market_quote_to_nav_record(product, quote)
    assert record.unit_nav == Decimal("1.0321")
    assert record.nav_date == date(2026, 7, 29)


def test_cash_market_quote_cannot_be_forced_into_nav_record():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    with pytest.raises(ValueError, match="no unit NAV"):
        market_quote_to_nav_record(
            MarketProduct("nanyin_wealth", "NYRR000007", "日日聚宝", ProductType.CASH_MANAGEMENT),
            MarketQuote(
                "NYRR000007", date(2026, 7, 30), "nanyin_wealth", "hash",
                income_per_10k=Decimal("0.4475"),
            ),
        )
```

- [ ] **Step 2: Verify the tests fail**

Run: `pytest tests/test_nav_monitor.py -q`

Expected: FAIL because `market_quote_to_nav_record` is missing.

- [ ] **Step 3: Implement the explicit compatibility adapter**

```python
def market_quote_to_nav_record(product, quote) -> NavRecord:
    from src.portfolio_models import ProductType

    if product.product_type is ProductType.CASH_MANAGEMENT:
        raise ValueError("cash management quote has no unit NAV")
    if quote.unit_nav is None:
        raise ValueError("market quote is missing unit NAV")
    return NavRecord(
        provider=product.provider,
        code=product.code,
        name=product.name,
        nav_date=quote.quote_date,
        unit_nav=quote.unit_nav,
        cumulative_nav=quote.cumulative_nav,
        source=quote.source,
    )
```

- [ ] **Step 4: Run all phase-2 focused tests**

Run: `pytest tests/test_portfolio_market.py tests/test_portfolio_providers.py tests/test_nav_monitor.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Run full regression and commit**

Run: `pytest -q`

Expected: all tests PASS.

```bash
git add src/nav_monitor.py tests/test_nav_monitor.py
git commit -m "refactor: bridge portfolio quotes to nav reports"
```

## Phase 2 Completion Gate

- `003103` and `015736` fixture tests prove exact date/NAV pairing.
- Nanyin and Citic cash fixtures prove unit NAV is not populated.
- Quote sync is idempotent by `(product_id, quote_date)`.
- Run `git diff --check`.
- Run all Phase 1 and Phase 2 tests, then `pytest -q`.
- No production network request is allowed in unit tests; inject fake clients/openers.
