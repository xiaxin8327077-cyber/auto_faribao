from datetime import date, datetime
from decimal import Decimal

from src.config import Config
from src.nav_dashboard import (
    NavDashboardStore,
    request_manual_refresh,
    should_auto_refresh,
)
from src.nav_monitor import NavProduct, NavRecord, ProductNavResult, query_nav_products


def _dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def _record(day: str, nav: str, code: str = "P1") -> NavRecord:
    return NavRecord(
        provider="citic_wealth",
        code=code,
        name=f"产品{code}",
        nav_date=date.fromisoformat(day),
        unit_nav=Decimal(nav),
    )


def _result(
    latest_day: str,
    latest_nav: str,
    previous_day: str,
    previous_nav: str,
    shares: str = "10000",
    code: str = "P1",
) -> ProductNavResult:
    return ProductNavResult(
        product=NavProduct("citic_wealth", code, f"产品{code}"),
        latest=_record(latest_day, latest_nav, code),
        previous=_record(previous_day, previous_nav, code),
        shares=Decimal(shares),
    )


def _cfg(shares: str = "10000", include_product: bool = True) -> Config:
    products = []
    if include_product:
        products.append(
            {
                "provider": "citic_wealth",
                "code": "P1",
                "name": "产品P1",
                "shares": shares,
            }
        )
    return Config({"nav_monitor": {"products": products}})


def test_initial_backfill_uses_current_shares_from_base_date(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")

    store.initialize_from_history(
        _cfg(),
        {
            "citic_wealth:P1": [
                _record("2026-06-30", "1.0000"),
                _record("2026-07-01", "1.0010"),
                _record("2026-07-02", "1.0030"),
            ]
        },
        _dt("2026-07-10 10:00"),
    )

    assert Decimal(store.payload(_cfg())["cumulative_profit"]) == Decimal("30.0000")


def test_share_change_only_affects_future_nav(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-01", "1.0010")]},
        _dt("2026-07-01 10:00"),
    )
    store.record_results(
        _cfg(),
        [_result("2026-07-02", "1.0020", "2026-07-01", "1.0010")],
        _dt("2026-07-02 10:00"),
    )

    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-02 11:00"))
    store.record_results(
        changed_cfg,
        [_result("2026-07-03", "1.0030", "2026-07-02", "1.0020", shares="20000")],
        _dt("2026-07-03 10:00"),
    )

    assert Decimal(store.payload(changed_cfg)["cumulative_profit"]) == Decimal("30.0000")


def test_deleted_product_keeps_historical_profit(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    cfg = _cfg()
    store.initialize_from_history(
        cfg,
        {"citic_wealth:P1": [_record("2026-07-01", "1.0010")]},
        _dt("2026-07-01 10:00"),
    )
    store.record_results(
        cfg,
        [_result("2026-07-02", "1.0020", "2026-07-01", "1.0010")],
        _dt("2026-07-02 10:00"),
    )
    before = store.payload(cfg)["cumulative_profit"]

    empty_cfg = _cfg(include_product=False)
    store.sync_portfolio(empty_cfg, _dt("2026-07-03 10:00"))

    assert store.payload(empty_cfg)["cumulative_profit"] == before
    assert store.payload(empty_cfg)["products"] == []


def test_new_product_does_not_backfill_before_add_date(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    empty_cfg = _cfg(include_product=False)
    store.initialize_from_history(empty_cfg, {}, _dt("2026-07-01 10:00"))

    cfg = _cfg()
    store.sync_portfolio(cfg, _dt("2026-07-05 10:00"))
    store.record_history(
        cfg,
        {
            "citic_wealth:P1": [
                _record("2026-07-04", "1.0000"),
                _record("2026-07-05", "1.0100"),
                _record("2026-07-06", "1.0200"),
            ]
        },
        _dt("2026-07-06 10:00"),
    )

    assert Decimal(store.payload(cfg)["cumulative_profit"]) == Decimal("100.0000")


def test_duplicate_nav_date_is_not_counted_twice(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    cfg = _cfg()
    store.initialize_from_history(
        cfg,
        {"citic_wealth:P1": [_record("2026-07-01", "1.0010")]},
        _dt("2026-07-01 10:00"),
    )
    item = _result("2026-07-02", "1.0020", "2026-07-01", "1.0010")

    store.record_results(cfg, [item], _dt("2026-07-02 10:00"))
    store.record_results(cfg, [item], _dt("2026-07-02 11:00"))

    assert len(store.state["profit_entries"]) == 1


def test_daily_profit_totals_group_products_and_sort_newest_first(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.state["profit_entries"] = [
        {"nav_date": "2026-07-02", "amount": "12.34"},
        {"nav_date": "2026-07-02", "amount": "-2.14"},
        {"nav_date": "2026-07-03", "amount": "-5.00"},
    ]

    payload = store.payload(_cfg(), now=_dt("2026-07-10 10:00"))

    assert payload["daily_profits"] == [
        {"date": "2026-07-03", "amount": "-5.00"},
        {"date": "2026-07-02", "amount": "10.20"},
    ]
    assert Decimal(payload["cumulative_profit"]) == Decimal("5.20")


def test_latest_profit_uses_latest_nav_date_not_discovery_date(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.state["profit_entries"] = [
        {
            "product_key": "citic_wealth:P1",
            "nav_date": "2026-07-08",
            "discovered_date": "2026-07-10",
            "amount": "-100.00",
        },
        {
            "product_key": "citic_wealth:P1",
            "nav_date": "2026-07-09",
            "discovered_date": "2026-07-10",
            "amount": "-20.00",
        },
        {
            "product_key": "citic_wealth:P2",
            "nav_date": "2026-07-09",
            "discovered_date": "2026-07-10",
            "amount": "5.00",
        },
        {
            "product_key": "citic_wealth:P3",
            "nav_date": "2026-06-30",
            "discovered_date": "2026-07-10",
            "amount": "100.00",
        },
    ]

    payload = store.payload(_cfg(), now=_dt("2026-07-10 18:00"))

    assert payload["latest_profit_date"] == "2026-07-09"
    assert payload["latest_profit"] == "-15.00"
    assert [item["amount"] for item in payload["negative_rankings"]] == ["-20.00"]
    assert [item["amount"] for item in payload["positive_rankings"]] == ["5.00"]
    assert [item["amount"] for item in payload["monthly_negative_rankings"]] == ["-120.00"]
    assert [item["amount"] for item in payload["monthly_positive_rankings"]] == ["5.00"]


def test_period_profit_totals_use_natural_months_and_years(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.state["profit_entries"] = [
        {"nav_date": "2026-07-01", "amount": "-2.00"},
        {"nav_date": "2026-07-31", "amount": "10.00"},
        {"nav_date": "2026-08-01", "amount": "3.00"},
        {"nav_date": "2027-01-02", "amount": "4.00"},
    ]

    payload = store.payload(_cfg(), now=_dt("2027-01-10 10:00"))

    assert payload["monthly_profits"] == [
        {"period": "2027-01", "amount": "4.00"},
        {"period": "2026-08", "amount": "3.00"},
        {"period": "2026-07", "amount": "8.00"},
    ]
    assert payload["yearly_profits"] == [
        {"period": "2027", "amount": "4.00"},
        {"period": "2026", "amount": "11.00"},
    ]
    assert payload["current_month_profit"] == "4.00"


def test_manual_monthly_profits_affect_totals_but_not_daily_rows(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.state["profit_entries"] = [
        {"nav_date": "2026-07-01", "amount": "-100.00"},
    ]
    historical = [
        {"period": "2026-01", "amount": "10.00"},
        {"period": "2026-02", "amount": "20.00"},
    ]

    store.set_manual_monthly_profits(historical, updated_at=_dt("2026-07-10 16:00"))
    store.set_manual_monthly_profits(historical, updated_at=_dt("2026-07-10 16:05"))
    payload = store.payload(_cfg(), now=_dt("2026-07-10 16:10"))

    assert payload["base_date"] == "2026-01-01"
    assert payload["cumulative_profit"] == "-70.00"
    assert payload["daily_profits"] == [
        {"date": "2026-07-01", "amount": "-100.00"},
    ]
    assert payload["monthly_profits"] == [
        {"period": "2026-07", "amount": "-100.00"},
        {"period": "2026-02", "amount": "20.00"},
        {"period": "2026-01", "amount": "10.00"},
    ]
    assert payload["yearly_profits"] == [
        {"period": "2026", "amount": "-70.00"},
    ]
    assert len(store.state["manual_period_profits"]) == 2


class _Provider:
    def fetch_latest(self, product, **kwargs):
        return [
            _record("2026-07-02", "1.0020", product.code),
            _record("2026-07-01", "1.0010", product.code),
        ]


def test_enterprise_latest_query_publishes_same_results_to_dashboard(monkeypatch):
    observed = []
    monkeypatch.setattr("src.nav_monitor.get_provider", lambda _provider: _Provider())
    monkeypatch.setattr(
        "src.nav_dashboard.observe_enterprise_results",
        lambda _cfg, results, **_kwargs: observed.extend(results),
    )

    results = query_nav_products(_cfg(), query_source="enterprise")

    assert observed == results


def test_dashboard_query_does_not_publish_as_enterprise(monkeypatch):
    monkeypatch.setattr("src.nav_monitor.get_provider", lambda _provider: _Provider())
    monkeypatch.setattr(
        "src.nav_dashboard.observe_enterprise_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    results = query_nav_products(_cfg(), query_source="dashboard")

    assert results[0].latest.nav_date == date(2026, 7, 2)


def test_enterprise_observer_runs_before_shared_query_lock_is_released(monkeypatch):
    from src.nav_monitor import nav_query_in_progress

    observed_lock_states = []
    monkeypatch.setattr("src.nav_monitor.get_provider", lambda _provider: _Provider())
    monkeypatch.setattr(
        "src.nav_dashboard.observe_enterprise_results",
        lambda *_args, **_kwargs: observed_lock_states.append(nav_query_in_progress()),
    )

    query_nav_products(_cfg(), query_source="enterprise")

    assert observed_lock_states == [True]


def test_auto_refresh_waits_when_enterprise_push_is_within_one_hour():
    state = {"initialized": True, "last_success_at": "2026-07-10T05:00:00"}

    assert should_auto_refresh(state, _dt("2026-07-10 07:30"), _cfg()) is False


def test_auto_refresh_runs_after_one_hour_away_from_enterprise_push():
    state = {"initialized": True, "last_success_at": "2026-07-10T08:08:00"}

    assert should_auto_refresh(state, _dt("2026-07-10 10:30"), _cfg()) is True


def test_manual_refresh_can_run_again_without_cooldown(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    launches = []
    monkeypatch.setattr(
        "src.nav_dashboard._launch_refresh_thread",
        lambda *_args, **_kwargs: launches.append(True) or True,
    )

    first = request_manual_refresh(_cfg(), now=_dt("2026-07-10 10:00"), state_path=state_path)
    second = request_manual_refresh(_cfg(), now=_dt("2026-07-10 10:01"), state_path=state_path)

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert launches == [True, True]
    assert "cooldown_until" not in second


def test_manual_refresh_does_not_reuse_recent_success(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    store = NavDashboardStore(state_path)
    store.state["last_success_at"] = "2026-07-10T09:58:00"
    store.save()
    launches = []
    monkeypatch.setattr(
        "src.nav_dashboard._launch_refresh_thread",
        lambda *_args, **_kwargs: launches.append(True) or True,
    )

    result = request_manual_refresh(
        _cfg(),
        now=_dt("2026-07-10 10:00"),
        state_path=state_path,
    )

    assert result == {"accepted": True, "status": "refreshing"}
    assert launches == [True]


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
    assert daily["2026-08-03"]["amount"] == "5.0"
    assert daily["2026-08-03"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "10.0"},
        {"name": "产品乙", "code": "P2", "amount": "-5.0"},
    ]
    assert daily["2026-09-01"]["amount"] == "0.00"
    assert daily["2026-09-01"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "5.00"},
        {"name": "产品乙", "code": "P2", "amount": "-5.0"},
    ]
    assert all(row.get("products") is not None for row in payload["daily_profits"])

    monthly = {row["period"]: row for row in payload["monthly_profits"]}
    assert monthly["2026-08"]["amount"] == "5.0"
    assert monthly["2026-08"]["products"] == daily["2026-08-03"]["products"]
    assert monthly["2026-09"]["products"] == daily["2026-09-01"]["products"]

    yearly = {row["period"]: row for row in payload["yearly_profits"]}
    assert yearly["2026"]["products"] == [
        {"name": "产品甲", "code": "P1", "amount": "15.00"},
        {"name": "产品乙", "code": "P2", "amount": "-10.0"},
    ]
    for row in (
        *payload["daily_profits"],
        *payload["monthly_profits"],
        *payload["yearly_profits"],
    ):
        assert sum(Decimal(item["amount"]) for item in row["products"]) == Decimal(
            row["amount"]
        )


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
        "-5.0"
    ]
    assert [item["code"] for item in payload["monthly_negative_rankings"]] == ["P2"]
