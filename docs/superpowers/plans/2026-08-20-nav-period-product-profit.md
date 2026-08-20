# 收益明细按产品展开 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在理财组合看板的每日/每月/每年收益明细里，点开一行即可展开该时段各产品收益。

**Architecture:** 用现有 `calculate_latest_profit(..., bundle_non_trading_days=False)` 先拆出每只产品每一天的收益，再滚成日/月/年行上的 `products` 数组。前端只改收益弹层：可展开行用手风琴，同时只开一行。本月排行改为读取当前自然月那一行的 `products`。

**Tech Stack:** Python, Flask, vanilla HTML/CSS/JavaScript, pytest, SQLite 组合账本

## Global Constraints

- 不改收益账本、计提、净值抓取、企微日报/晚报/周期报告。
- 不新开 API；沿用 `GET /api/nav-dashboard`。
- 日期截断仍为 `2026-08-01`；产品范围仍为 `list_products(active_only=True)`。
- `sum(products.amount) == row.amount`；金额为 0 的产品不进 `products`。
- 正收益橙红、负收益绿；名称用现有 `shortName`。
- 不把「本月累计」做成第二入口；不做月份选择器。
- 提交时不要改 git config。若 `git commit` 因缺少 user.name 失败，用环境变量套用仓库最近一次提交的 author，不要 `git config`。

## File Structure

- Modify: `src/nav_dashboard.py` — 按产品拆每日收益，挂到日/月/年行，修正本月排行
- Modify: `src/nav_dashboard_page.html` — 弹层行展开/收起
- Test: `tests/test_nav_dashboard.py` — 拆分、加总、排序、排行口径
- Test: `tests/test_nav_dashboard_page.py` — 展开控件与手风琴脚本
- Test: `tests/test_nav_dashboard_routes.py` — 弹层结构仍在

---

### Task 1: 产品明细纯函数

**Files:**
- Modify: `src/nav_dashboard.py`
- Test: `tests/test_nav_dashboard.py`

**Interfaces:**
- Consumes: `Decimal` 金额、带 `name`/`code` 的产品对象
- Produces:
  - `_sum_product_amounts(daily_product_profits: dict, days: list[str]) -> dict[str, Decimal]`
  - `_product_breakdown_rows(amounts_by_id: dict, products_by_id: dict) -> list[dict]`，每项为 `{"name", "code", "amount"}`，`amount` 为 `format(value, "f")` 字符串，按金额从高到低，省略恰好为 0 的产品

- [ ] **Step 1: Write the failing test**

在 `tests/test_nav_dashboard.py` 末尾追加：

```python
from types import SimpleNamespace
from src.nav_dashboard import _product_breakdown_rows, _sum_product_amounts


def test_product_breakdown_omits_zero_and_sorts_high_to_low():
    products = {
        "a": SimpleNamespace(name="产品甲", code="P1"),
        "b": SimpleNamespace(name="产品乙", code="P2"),
        "c": SimpleNamespace(name="产品丙", code="P3"),
    }
    rows = _product_breakdown_rows(
        {
            "a": Decimal("10"),
            "b": Decimal("-5"),
            "c": Decimal("0"),
        },
        products,
    )
    assert rows == [
        {"name": "产品甲", "code": "P1", "amount": "10"},
        {"name": "产品乙", "code": "P2", "amount": "-5"},
    ]


def test_sum_product_amounts_adds_the_same_product_across_days():
    daily = {
        "2026-08-03": {"a": Decimal("10"), "b": Decimal("-5")},
        "2026-08-04": {"a": Decimal("2")},
        "2026-09-01": {"a": Decimal("5"), "b": Decimal("-5")},
    }
    assert _sum_product_amounts(daily, ["2026-08-03", "2026-08-04"]) == {
        "a": Decimal("12"),
        "b": Decimal("-5"),
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_nav_dashboard.py::test_product_breakdown_omits_zero_and_sorts_high_to_low tests/test_nav_dashboard.py::test_sum_product_amounts_adds_the_same_product_across_days -v`

Expected: FAIL，报 `ImportError` 或 `cannot import name '_product_breakdown_rows'`。

- [ ] **Step 3: Write minimal implementation**

在 `src/nav_dashboard.py` 的 `_portfolio_daily_profit_totals` 上方加入：

```python
def _sum_product_amounts(daily_product_profits, days) -> dict:
    totals = {}
    for day in days:
        for product_id, amount in (daily_product_profits.get(day) or {}).items():
            totals[product_id] = totals.get(product_id, Decimal("0")) + amount
    return totals


def _product_breakdown_rows(amounts_by_id, products_by_id) -> list:
    rows = []
    for product_id, amount in amounts_by_id.items():
        if amount == 0:
            continue
        product = products_by_id.get(product_id)
        if product is None:
            continue
        rows.append(
            {
                "name": product.name,
                "code": product.code,
                "amount": format(amount, "f"),
            }
        )
    rows.sort(key=lambda item: Decimal(item["amount"]), reverse=True)
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_nav_dashboard.py::test_product_breakdown_omits_zero_and_sorts_high_to_low tests/test_nav_dashboard.py::test_sum_product_amounts_adds_the_same_product_across_days -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_nav_dashboard.py src/nav_dashboard.py
git commit -m "$(cat <<'EOF'
feat: 增加收益明细按产品汇总的纯函数。

EOF
)"
```

---

### Task 2: 日/月/年行挂上 products

**Files:**
- Modify: `src/nav_dashboard.py`
- Test: `tests/test_nav_dashboard.py`

**Interfaces:**
- Consumes: Task 1 的 `_sum_product_amounts`、`_product_breakdown_rows`；`calculate_latest_profit(..., bundle_non_trading_days=False)`；`repository.list_products(active_only=True)` / `list_quotes`
- Produces:
  - `_portfolio_daily_product_profits(repository, as_of: date) -> dict[str, dict[str, Decimal]]`，键为 `YYYY-MM-DD`，值为 `{product_id: amount}`
  - `_portfolio_daily_profit_totals(repository, as_of)` 改为对该 map 求和，对外返回值形状不变
  - `_apply_portfolio_profit_views` 写出的 `daily_profits` / `monthly_profits` / `yearly_profits` 每一行都带 `products` 数组（无明细时为 `[]`）

- [ ] **Step 1: Write the failing integration test**

在 `tests/test_nav_dashboard.py` 追加（放在 Task 1 测试后面）：

```python
from src.nav_dashboard import _apply_portfolio_profit_views
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_repository import PortfolioRepository


def _period_profit_repository(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    products = (
        Product("a", "citic_wealth", "P1", "产品甲", ProductType.WEALTH_NAV),
        Product("b", "citic_wealth", "P2", "产品乙", ProductType.WEALTH_NAV),
        Product("c", "citic_wealth", "P3", "产品丙", ProductType.WEALTH_NAV),
    )
    shares = {"a": Decimal("100"), "b": Decimal("50"), "c": Decimal("10")}
    quotes = {
        "a": (
            (date(2026, 8, 1), Decimal("1.00")),
            (date(2026, 8, 3), Decimal("1.10")),
            (date(2026, 9, 1), Decimal("1.15")),
        ),
        "b": (
            (date(2026, 8, 1), Decimal("1.00")),
            (date(2026, 8, 3), Decimal("0.90")),
            (date(2026, 9, 1), Decimal("0.80")),
        ),
        "c": (
            (date(2026, 8, 1), Decimal("1.00")),
            (date(2026, 8, 3), Decimal("1.00")),
        ),
    }
    for product in products:
        repository.add_product(product)
        repository.create_transaction(
            Transaction(
                id=f"open:{product.id}",
                product_id=product.id,
                transaction_type=TransactionType.OPENING_POSITION,
                status=TransactionStatus.CONFIRMED,
                trade_date=date(2026, 8, 1),
                confirmation_date=date(2026, 8, 1),
                idempotency_key=f"open:{product.id}",
                amount=shares[product.id],
                shares=shares[product.id],
                confirmation_nav=Decimal("1"),
            )
        )
        for quote_date, unit_nav in quotes[product.id]:
            repository.upsert_quote(
                product.id,
                MarketQuote(
                    product.code,
                    quote_date,
                    "official",
                    f"{product.id}-{quote_date.isoformat()}",
                    unit_nav=unit_nav,
                ),
                f"{quote_date.isoformat()}T10:00:00",
            )
    return repository


def test_apply_portfolio_profit_views_attaches_products_that_sum_to_each_row(tmp_path):
    repository = _period_profit_repository(tmp_path)
    payload = {"daily_profits": [], "monthly_profits": [], "yearly_profits": []}
    portfolio = {
        "summary": {"as_of": "2026-09-02"},
        "products": [
            {"id": "a", "name": "产品甲", "code": "P1"},
            {"id": "b", "name": "产品乙", "code": "P2"},
            {"id": "c", "name": "产品丙", "code": "P3"},
        ],
    }

    _apply_portfolio_profit_views(payload, repository, portfolio)

    daily = {row["date"]: row for row in payload["daily_profits"]}
    assert daily["2026-08-03"]["amount"] == "5.00"
    assert daily["2026-08-03"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "10.00"},
        {"name": "产品乙", "code": "P2", "amount": "-5.00"},
    ]
    assert daily["2026-09-01"]["amount"] == "0.00"
    assert daily["2026-09-01"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "5.00"},
        {"name": "产品乙", "code": "P2", "amount": "-5.00"},
    ]
    assert all(row.get("products") is not None for row in payload["daily_profits"])

    monthly = {row["period"]: row for row in payload["monthly_profits"]}
    assert monthly["2026-08"]["amount"] == "5.00"
    assert monthly["2026-08"]["products"] == daily["2026-08-03"]["products"]
    assert monthly["2026-09"]["products"] == daily["2026-09-01"]["products"]

    yearly = {row["period"]: row for row in payload["yearly_profits"]}
    assert yearly["2026"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "15.00"},
        {"name": "产品乙", "code": "P2", "amount": "-10.00"},
    ]
    for row in (
        *payload["daily_profits"],
        *payload["monthly_profits"],
        *payload["yearly_profits"],
    ):
        assert sum(Decimal(item["amount"]) for item in row["products"]) == Decimal(
            row["amount"]
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_nav_dashboard.py::test_apply_portfolio_profit_views_attaches_products_that_sum_to_each_row -v`

Expected: FAIL，因为现有日/月/年行没有 `products`。

- [ ] **Step 3: Write minimal implementation**

把 `_portfolio_daily_profit_totals` 改成基于产品日明细：

```python
def _portfolio_daily_product_profits(repository, as_of: date) -> dict:
    from collections import defaultdict

    from src.portfolio_profit import calculate_latest_profit

    daily = defaultdict(dict)
    for product in repository.list_products(active_only=True):
        quote_dates = sorted(
            {
                quote.quote_date
                for quote in repository.list_quotes(
                    product.id,
                    on_or_before=as_of,
                )
            }
        )
        for quote_date in quote_dates:
            profit_date, profit = calculate_latest_profit(
                repository,
                product,
                quote_date,
                bundle_non_trading_days=False,
            )
            if profit_date == quote_date and profit is not None:
                day = quote_date.isoformat()
                daily[day][product.id] = (
                    daily[day].get(product.id, Decimal("0")) + profit
                )
    return dict(daily)


def _portfolio_daily_profit_totals(repository, as_of: date) -> dict:
    return {
        day: sum(amounts.values(), Decimal("0"))
        for day, amounts in _portfolio_daily_product_profits(
            repository, as_of
        ).items()
    }
```

改 `_apply_portfolio_profit_views`：用 `_portfolio_daily_product_profits` 代替只求和的 totals；在写出 `daily_profits` / `monthly_profits` / `yearly_profits` 时为每一行填 `products`。保留现有 cutoff（`2026-08-01` / `2026-08`）和 ledger 覆盖 legacy 的合并逻辑。关键片段：

```python
    try:
        ledger_daily_products = _portfolio_daily_product_profits(repository, as_of)
    except Exception:
        logger.exception("Failed to rebuild dashboard daily profits from ledger")
        return

    ledger_daily = {
        day: sum(amounts.values(), Decimal("0"))
        for day, amounts in ledger_daily_products.items()
    }
    # ... 现有 merged_daily / cutoff 逻辑保持不变 ...

    products_by_id = {
        product.id: product
        for product in repository.list_products(active_only=True)
    }

    def products_for(days):
        return _product_breakdown_rows(
            _sum_product_amounts(ledger_daily_products, days),
            products_by_id,
        )

    payload["daily_profits"] = [
        {
            "date": day,
            "amount": format(amount, "f"),
            "products": products_for([day]),
        }
        for day, amount in sorted(merged_daily.items(), reverse=True)
    ]

    monthly_days = {}
    for day in merged_daily:
        monthly_days.setdefault(day[:7], []).append(day)
    payload["monthly_profits"] = [
        {
            "period": period,
            "amount": format(amount, "f"),
            "products": products_for(monthly_days[period]),
        }
        for period, amount in sorted(monthly_map.items(), reverse=True)
    ]

    yearly_days = {}
    for day in merged_daily:
        yearly_days.setdefault(day[:4], []).append(day)
    payload["yearly_profits"] = [
        {
            "period": year,
            "amount": format(amount, "f"),
            "products": products_for(yearly_days[year]),
        }
        for year, amount in sorted(yearly_map.items(), reverse=True)
    ]
```

只对 ledger 里有的产品做拆分。某日若只存在于 legacy `daily_profits`、ledger 没有，则 `products_for` 得到 `[]`，该行不可展开。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_nav_dashboard.py::test_apply_portfolio_profit_views_attaches_products_that_sum_to_each_row tests/test_nav_dashboard.py::test_daily_profit_totals_group_products_and_sort_newest_first tests/test_nav_dashboard.py::test_period_profit_totals_use_natural_months_and_years -v`

Expected: PASS。后两个是 `store.payload()` 旧路径，不应被这次改动破坏。

- [ ] **Step 5: Commit**

```bash
git add tests/test_nav_dashboard.py src/nav_dashboard.py
git commit -m "$(cat <<'EOF'
feat: 日/月/年收益行附带各产品明细。

EOF
)"
```

---

### Task 3: 本月排行改用当月 products

**Files:**
- Modify: `src/nav_dashboard.py` 中 `_apply_portfolio_rankings`
- Test: `tests/test_nav_dashboard.py`

**Interfaces:**
- Consumes: Task 2 写出的 `payload["monthly_profits"]` 各行之 `products`；`as_of.strftime("%Y-%m")`
- Produces: `monthly_positive_rankings` / `monthly_negative_rankings` 为当前自然月 `products` 里正负各最多 3 条，形状仍为 `{name, code, amount}`

- [ ] **Step 1: Write the failing test**

```python
def test_monthly_rankings_use_current_month_products_not_since_august(tmp_path):
    repository = _period_profit_repository(tmp_path)
    payload = {"daily_profits": [], "monthly_profits": [], "yearly_profits": []}
    portfolio = {
        "summary": {"as_of": "2026-09-02"},
        "products": [
            {"id": "a", "name": "产品甲", "code": "P1"},
            {"id": "b", "name": "产品乙", "code": "P2"},
            {"id": "c", "name": "产品丙", "code": "P3"},
        ],
    }

    _apply_portfolio_profit_views(payload, repository, portfolio)

    assert [item["amount"] for item in payload["monthly_positive_rankings"]] == [
        "5.00"
    ]
    assert [item["code"] for item in payload["monthly_positive_rankings"]] == ["P1"]
    assert [item["amount"] for item in payload["monthly_negative_rankings"]] == [
        "-5.00"
    ]
    assert [item["code"] for item in payload["monthly_negative_rankings"]] == ["P2"]
```

当前实现把 8 月以来累计（甲 `15.00`、乙 `-10.00`）当成当月，这个测试应当失败。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_nav_dashboard.py::test_monthly_rankings_use_current_month_products_not_since_august -v`

Expected: FAIL，正贡献金额是 `15.00` 而不是 `5.00`。

- [ ] **Step 3: Write minimal implementation**

改 `_apply_portfolio_rankings` 的月度部分：不要再按全部 quote 累加。改为：

```python
    current_month = as_of.strftime("%Y-%m")
    month_row = next(
        (
            row
            for row in payload.get("monthly_profits") or []
            if row.get("period") == current_month
        ),
        None,
    )
    month_products = list(month_row.get("products") or []) if month_row else []
    payload["monthly_positive_rankings"] = [
        item for item in month_products if Decimal(item["amount"]) > 0
    ][:3]
    payload["monthly_negative_rankings"] = [
        item for item in reversed(month_products) if Decimal(item["amount"]) < 0
    ][:3]
```

每日排行（`positive_rankings` / `negative_rankings`）保持现有逻辑，本任务不要改。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_nav_dashboard.py::test_monthly_rankings_use_current_month_products_not_since_august tests/test_nav_dashboard.py::test_latest_profit_uses_latest_nav_date_not_discovery_date -v`

Expected: PASS。第二个测试覆盖 `store.payload()` 的旧月度排行，不应被破坏。

- [ ] **Step 5: Commit**

```bash
git add tests/test_nav_dashboard.py src/nav_dashboard.py
git commit -m "$(cat <<'EOF'
fix: 本月排行改用当前自然月的产品收益。

EOF
)"
```

---

### Task 4: 弹层手风琴展开

**Files:**
- Modify: `src/nav_dashboard_page.html`
- Test: `tests/test_nav_dashboard_page.py`
- Test: `tests/test_nav_dashboard_routes.py`

**Interfaces:**
- Consumes: Task 2 的 `item.products`；现有 `profitViews` / `renderDailyProfits` / `setProfitView` / `closeDailyProfits`
- Produces: 有 `products.length > 0` 的行渲染为 `button[data-profit-row-key][aria-expanded]`；展开区 `.daily-profit-products`；`expandedProfitKey` 同时只保存一行；切 Tab 和关弹层时清空

- [ ] **Step 1: Write the failing page tests**

在 `tests/test_nav_dashboard_page.py` 追加：

```python
def test_profit_sheet_expands_one_period_row_at_a_time():
    html = _read_page()
    assert "let expandedProfitKey = '';" in html
    assert "function toggleProfitRow(key)" in html
    assert "data-profit-row-key" in html
    assert "aria-expanded=" in html
    assert "daily-profit-products" in html
    assert "daily-profit-product-name" in html
    assert "expandedProfitKey = expandedProfitKey === key ? '' : key;" in html
    assert "profit-row-chevron" in html
    assert "const expandable = products.length > 0;" in html


def test_profit_sheet_clears_expanded_row_on_tab_change_and_close():
    html = _read_page()
    assert "function setProfitView(view)" in html
    assert "function closeDailyProfits()" in html
    close_idx = html.index("function closeDailyProfits()")
    set_idx = html.index("function setProfitView(view)")
    assert "expandedProfitKey = '';" in html[set_idx:set_idx + 220]
    assert "expandedProfitKey = '';" in html[close_idx:close_idx + 420]
```

在 `tests/test_nav_dashboard_routes.py` 的 `test_nav_page_contains_daily_profit_bottom_sheet` 末尾追加：

```python
    assert "data-profit-row-key" in html
    assert "daily-profit-products" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_nav_dashboard_page.py::test_profit_sheet_expands_one_period_row_at_a_time tests/test_nav_dashboard_page.py::test_profit_sheet_clears_expanded_row_on_tab_change_and_close tests/test_nav_dashboard_routes.py::test_nav_page_contains_daily_profit_bottom_sheet -v`

Expected: FAIL，页面还没有展开标记。

- [ ] **Step 3: Implement the sheet interaction**

改 `src/nav_dashboard_page.html` 里现有 `.daily-profit-row` 规则（约 274–275 行），不要另写一条覆盖 `display: grid` 的同名规则：

```css
    .daily-profit-group { border-bottom: 1px solid var(--line-soft); }
    .daily-profit-group:last-child { border-bottom: 0; }
    .daily-profit-row {
      display: grid; grid-template-columns: 1fr auto; align-items: center;
      width: 100%; min-height: 48px; margin: 0; padding: 0; border: 0;
      background: transparent; color: inherit; font: inherit; text-align: left;
    }
    button.daily-profit-row { cursor: pointer; }
    button.daily-profit-row:focus-visible { outline: 3px solid rgba(31, 111, 209, .22); outline-offset: 1px; }
    .profit-row-chevron {
      display: inline-block; width: 0; height: 0; margin-right: 8px;
      border-top: 5px solid transparent; border-bottom: 5px solid transparent;
      border-left: 6px solid #8aa0b8; vertical-align: middle;
    }
    .daily-profit-row.expanded .profit-row-chevron { transform: rotate(90deg); }
    .daily-profit-products { padding: 0 0 8px 22px; }
    .daily-profit-product { display: grid; grid-template-columns: 1fr auto; align-items: center; min-height: 36px; }
    .daily-profit-product-name { color: var(--muted); font-size: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .daily-profit-product .daily-profit-amount { font-size: 12px; }
```

底边改由 `.daily-profit-group` 画，避免展开后双线。删除旧的 `.daily-profit-row:last-child { border-bottom: 0; }`。

在 `let profitViews = {daily: [], monthly: [], yearly: []};` 旁增加 `let expandedProfitKey = '';`。

替换 `renderDailyProfits` / `setProfitView` / `closeDailyProfits`，并增加 `profitRowKey`、`toggleProfitRow`：

```javascript
    function profitRowKey(item) {
      return activeProfitView === 'daily' ? `daily:${item.date || ''}` : `${activeProfitView}:${item.period || ''}`;
    }
    function renderDailyProfits() {
      const meta = profitViewMeta[activeProfitView];
      const rows = profitViews[activeProfitView] || [];
      $('sheetCumulativeProfit').textContent = `${signedMoney(cumulativeProfit)} 元`;
      $('sheetCumulativeProfit').className = `sheet-summary-total ${tone(cumulativeProfit)}`;
      $('dailyProfitPeriod').innerHTML = `自 ${esc(profitBaseDate)} 起<br>${rows.length} ${meta.countLabel}`;
      $('profitPeriodColumn').textContent = meta.periodLabel;
      $('profitAmountColumn').textContent = meta.amountLabel;
      $('dailyProfitList').setAttribute('aria-labelledby', meta.tab);
      document.querySelectorAll('[data-profit-view]').forEach(tab => {
        tab.setAttribute('aria-selected', String(tab.dataset.profitView === activeProfitView));
      });
      const html = rows.length ? rows.map(item => {
        const products = Array.isArray(item.products) ? item.products : [];
        const key = profitRowKey(item);
        const expandable = products.length > 0;
        const expanded = expandable && expandedProfitKey === key;
        const amount = `<span class="daily-profit-amount ${tone(item.amount)}">${signedMoney(item.amount)} 元</span>`;
        const label = esc(profitPeriodLabel(item));
        const rowInner = expandable
          ? `<span class="daily-profit-date"><span class="profit-row-chevron" aria-hidden="true"></span>${label}</span>${amount}`
          : `<span class="daily-profit-date">${label}</span>${amount}`;
        const row = expandable
          ? `<button class="daily-profit-row${expanded ? ' expanded' : ''}" type="button" data-profit-row-key="${esc(key)}" aria-expanded="${expanded}">${rowInner}</button>`
          : `<div class="daily-profit-row">${rowInner}</div>`;
        const children = expanded
          ? `<div class="daily-profit-products">${products.map(product => `<div class="daily-profit-product"><span class="daily-profit-product-name">${esc(shortName(product.name || product.code))}</span><span class="daily-profit-amount ${tone(product.amount)}">${signedMoney(product.amount)} 元</span></div>`).join('')}</div>`
          : '';
        return `<div class="daily-profit-group">${row}${children}</div>`;
      }).join('') : '<div class="empty">暂无收益明细</div>';
      $('dailyProfitList').innerHTML = html;
    }
    function toggleProfitRow(key) {
      expandedProfitKey = expandedProfitKey === key ? '' : key;
      renderDailyProfits();
    }
    function setProfitView(view) {
      if (!profitViewMeta[view]) return;
      activeProfitView = view;
      expandedProfitKey = '';
      renderDailyProfits();
    }
    function closeDailyProfits() {
      if ($('dailyProfitBackdrop').hidden) return;
      expandedProfitKey = '';
      $('dailyProfitBackdrop').hidden = true;
      $('cumulativeProfitTrigger').setAttribute('aria-expanded', 'false');
      document.body.classList.remove('sheet-open');
      $('cumulativeProfitTrigger').focus();
    }
```

在已有 `$('dailyProfitClose').addEventListener('click', closeDailyProfits);` 附近增加：

```javascript
    $('dailyProfitList').addEventListener('click', event => {
      const button = event.target.closest('[data-profit-row-key]');
      if (!button) return;
      toggleProfitRow(button.dataset.profitRowKey);
    });
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_nav_dashboard_page.py::test_profit_sheet_expands_one_period_row_at_a_time tests/test_nav_dashboard_page.py::test_profit_sheet_clears_expanded_row_on_tab_change_and_close tests/test_nav_dashboard_routes.py::test_nav_page_contains_daily_profit_bottom_sheet -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nav_dashboard_page.html tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py
git commit -m "$(cat <<'EOF'
feat: 收益明细支持按行展开各产品收益。

EOF
)"
```

---

### Task 5: 回归

**Files:**
- Test: `tests/test_nav_dashboard.py`
- Test: `tests/test_nav_dashboard_page.py`
- Test: `tests/test_nav_dashboard_routes.py`

- [ ] **Step 1: Run the dashboard test files named in the spec**

Run: `pytest -q tests/test_nav_dashboard.py tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py`

Expected: PASS

- [ ] **Step 2: Run compile check**

Run: `python -m compileall -q src`

Expected: 无输出、退出码 0

- [ ] **Step 3: Commit only if Step 1–2 带出了额外修复**

若没有新改动，不要空提交。若有修复：

```bash
git add src tests
git commit -m "$(cat <<'EOF'
test: 对齐收益明细按产品展开的回归检查。

EOF
)"
```
