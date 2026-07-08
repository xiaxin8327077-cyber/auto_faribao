from datetime import date, datetime
from decimal import Decimal

from src.config import Config, save_config
from src.nav_monitor import (
    DateQueryResult,
    NavProduct,
    NavRecord,
    ProductNavResult,
    calculate_change,
    format_nav_report,
    parse_nav_query_date,
)


def test_nav_monitor_config_defaults_to_enabled_with_0800():
    cfg = Config({})

    assert cfg.nav_monitor.enabled is True
    assert cfg.nav_monitor.push_hour == 8
    assert cfg.nav_monitor.push_minute == 0
    assert cfg.nav_monitor.products == []


def test_save_config_preserves_nav_monitor(tmp_path):
    cfg = Config({
        "nav_monitor": {
            "enabled": True,
            "push_hour": 8,
            "push_minute": 30,
            "products": [
                {
                    "provider": "citic_wealth",
                    "code": "AF233276B",
                    "name": "P1",
                },
            ],
        }
    })
    path = tmp_path / "config.yaml"

    data = save_config(str(path), cfg, {"scheduler": {"report_submit_hour": 20}})

    assert data["scheduler"]["report_submit_hour"] == 20
    assert data["nav_monitor"]["enabled"] is True
    assert data["nav_monitor"]["push_hour"] == 8
    assert data["nav_monitor"]["push_minute"] == 30
    assert data["nav_monitor"]["products"][0]["provider"] == "citic_wealth"
    assert data["nav_monitor"]["products"][0]["code"] == "AF233276B"


def test_parse_relative_dates():
    base = date(2026, 7, 8)

    assert parse_nav_query_date("查询昨天净值", base) == date(2026, 7, 7)
    assert parse_nav_query_date("前天净值", base) == date(2026, 7, 6)
    assert parse_nav_query_date("今天净值", base) == date(2026, 7, 8)


def test_parse_compact_query_date():
    assert parse_nav_query_date("查询净值 20260707", date(2026, 7, 8)) == date(2026, 7, 7)


def test_calculate_change_and_latest_report_format():
    product = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")
    latest = NavRecord(
        provider="citic_wealth",
        code="AF233276B",
        name=product.name,
        nav_date=date(2026, 7, 7),
        unit_nav=Decimal("1.078000"),
        cumulative_nav=None,
        source="test",
    )
    previous = NavRecord(
        provider="citic_wealth",
        code="AF233276B",
        name=product.name,
        nav_date=date(2026, 7, 6),
        unit_nav=Decimal("1.078100"),
        cumulative_nav=None,
        source="test",
    )

    delta, delta_pct = calculate_change(latest, previous)
    text = format_nav_report(
        [ProductNavResult(product=product, latest=latest, previous=previous)],
        title="理财净值日报",
        generated_at=datetime(2026, 7, 8, 8, 0),
    )

    assert delta == Decimal("-0.000100")
    assert delta_pct.quantize(Decimal("0.0001")) == Decimal("-0.0093")
    assert "慧盈象固收增强一年持有期5号B" in text
    assert "2026-07-07  1.078000" in text
    assert "涨跌：-0.000100（-0.0093%）" in text


def test_date_miss_report_format():
    product = NavProduct("nanyin_wealth", "NYZY000022", "南银理财致远一年定开19期A份额")
    nearest = NavRecord(
        provider="nanyin_wealth",
        code="NYZY000022",
        name=product.name,
        nav_date=date(2026, 6, 26),
        unit_nav=Decimal("1.000000"),
        cumulative_nav=None,
        source="test",
    )

    text = format_nav_report(
        [
            ProductNavResult(
                product=product,
                query_result=DateQueryResult(
                    target_date=date(2026, 7, 2),
                    exact=False,
                    record=nearest,
                    previous=None,
                ),
            )
        ],
        title="净值查询",
        generated_at=datetime(2026, 7, 8, 8, 0),
        target_date=date(2026, 7, 2),
    )

    assert "查询日期：2026-07-02" in text
    assert "状态：该日未披露" in text
    assert "最近披露：2026-06-26  1.000000" in text
