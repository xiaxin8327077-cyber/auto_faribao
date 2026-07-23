from datetime import date, datetime
from decimal import Decimal

import pytest

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


def test_same_day_share_change_before_discovery_uses_new_shares(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-21", "1.0010")]},
        _dt("2026-07-21 10:00"),
    )
    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 14:00"))
    store.record_results(
        changed_cfg,
        [_result("2026-07-22", "1.0020", "2026-07-21", "1.0010", shares="20000")],
        _dt("2026-07-22 23:30"),
    )

    entry = store.state["profit_entries"][0]
    assert entry["shares"] == "20000"
    assert entry["amount"] == "20.0000"


def test_share_change_after_discovery_keeps_booked_profit(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    cfg = _cfg()
    item = _result("2026-07-22", "1.0020", "2026-07-21", "1.0010")
    store.initialize_from_history(
        cfg,
        {"citic_wealth:P1": [_record("2026-07-21", "1.0010")]},
        _dt("2026-07-21 10:00"),
    )
    store.record_results(cfg, [item], _dt("2026-07-22 10:00"))

    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 11:00"))
    store.record_results(changed_cfg, [item], _dt("2026-07-22 12:00"))

    assert len(store.state["profit_entries"]) == 1
    assert store.state["profit_entries"][0]["shares"] == "10000"
    assert store.state["profit_entries"][0]["amount"] == "10.0000"


def test_delayed_nav_disclosure_uses_shares_at_discovery_time(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-20", "1.0010")]},
        _dt("2026-07-20 10:00"),
    )
    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 14:00"))
    store.record_results(
        changed_cfg,
        [_result("2026-07-21", "1.0020", "2026-07-20", "1.0010", shares="20000")],
        _dt("2026-07-23 09:00"),
    )

    entry = store.state["profit_entries"][0]
    assert entry["shares"] == "20000"
    assert entry["amount"] == "20.0000"


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


def test_manual_refresh_has_global_five_minute_cooldown(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("src.nav_dashboard._launch_refresh_thread", lambda *_args, **_kwargs: True)

    first = request_manual_refresh(_cfg(), now=_dt("2026-07-10 10:00"), state_path=state_path)
    second = request_manual_refresh(_cfg(), now=_dt("2026-07-10 10:01"), state_path=state_path)

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["status"] == "cooldown"


def test_manual_refresh_reuses_any_recent_success(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    store = NavDashboardStore(state_path)
    store.state["last_success_at"] = "2026-07-10T09:58:00"
    store.save()
    monkeypatch.setattr(
        "src.nav_dashboard._launch_refresh_thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    result = request_manual_refresh(
        _cfg(),
        now=_dt("2026-07-10 10:00"),
        state_path=state_path,
    )

    assert result["accepted"] is False
    assert result["status"] == "cooldown"


def _store_with_entries(tmp_path, entries):
    store = NavDashboardStore(tmp_path / "state.json")
    store.state["profit_entries"] = entries
    store.state["base_date"] = "2026-07-01"
    store.save()
    return store


def test_set_manual_daily_profit_only_allows_max_nav_date(tmp_path):
    entries = [
        {"product_key": "p1", "nav_date": "2026-07-18", "amount": "100", "discovered_date": "2026-07-19"},
        {"product_key": "p1", "nav_date": "2026-07-21", "amount": "80", "discovered_date": "2026-07-22"},
    ]
    store = _store_with_entries(tmp_path, entries)

    # 最大收益日期可以修改
    store.set_manual_daily_profit(date(2026, 7, 21), Decimal("200"))
    assert store.state["manual_daily_profits"]["2026-07-21"]["amount"] == "200"


def test_set_manual_daily_profit_rejects_earlier_date(tmp_path):
    entries = [
        {"product_key": "p1", "nav_date": "2026-07-18", "amount": "100", "discovered_date": "2026-07-19"},
        {"product_key": "p1", "nav_date": "2026-07-21", "amount": "80", "discovered_date": "2026-07-22"},
    ]
    store = _store_with_entries(tmp_path, entries)

    # 比最大日期早一天必须拒绝
    with pytest.raises(ValueError, match="仅可修改看板最新收益日期"):
        store.set_manual_daily_profit(date(2026, 7, 20), Decimal("150"))


def test_set_manual_daily_profit_rejects_later_date(tmp_path):
    entries = [
        {"product_key": "p1", "nav_date": "2026-07-21", "amount": "80", "discovered_date": "2026-07-22"},
    ]
    store = _store_with_entries(tmp_path, entries)

    # 比最大日期晚一天必须拒绝
    with pytest.raises(ValueError, match="仅可修改看板最新收益日期"):
        store.set_manual_daily_profit(date(2026, 7, 22), Decimal("300"))


def test_set_manual_daily_profit_rejects_when_no_entries(tmp_path):
    store = _store_with_entries(tmp_path, [])

    # 看板没有收益数据时必须拒绝
    with pytest.raises(ValueError, match="看板尚无收益数据"):
        store.set_manual_daily_profit(date(2026, 7, 21), Decimal("100"))


def test_set_manual_daily_profit_repeat_does_not_stack(tmp_path):
    entries = [
        {"product_key": "p1", "nav_date": "2026-07-21", "amount": "80", "discovered_date": "2026-07-22"},
    ]
    store = _store_with_entries(tmp_path, entries)

    # 重复修改最大收益日期，累计/月度/年度收益不能重复叠加
    store.set_manual_daily_profit(date(2026, 7, 21), Decimal("200"))
    payload = store.payload(now=datetime(2026, 7, 22, 10, 0))
    assert payload["cumulative_profit"] == "200"

    store.set_manual_daily_profit(date(2026, 7, 21), Decimal("300"))
    payload = store.payload(now=datetime(2026, 7, 22, 10, 0))
    assert payload["cumulative_profit"] == "300"
    assert payload["latest_profit"] == "300"
    assert payload["current_month_profit"] == "300"
