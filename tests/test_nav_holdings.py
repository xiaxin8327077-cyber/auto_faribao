from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from src.config import Config
from src.nav_holdings import (
    HoldingProfile,
    MarketSnapshot,
    MarketSymbol,
    TopAsset,
    build_estimate_report,
    format_holdings_profiles,
    format_holdings_status,
    parse_holding_profile_from_text,
    target_quarter_for_check,
    update_profile_cache,
)
from src.nav_monitor import NavProduct


CITIC_SAMPLE = """
信银理财慧盈象固收增强一年持有期5号理财产品(产品代码：AF233276)2026年1季度运行公告
6.1报告期末理财产品持有资产情况
序号 资产类别
穿透前 穿透后
资产余额（元） 占穿透前总资产
的比例（%） 资产余额（元） 占穿透后总资产
的比例（%）
1 现金及银行存款 649,151,861.36 9.23 1,659,610,432.28 23.87
4 债券 944,643,395.31 13.43 4,129,145,213.93 59.40
6 权益类投资 - - 692,138,900.14 9.96
11 公募基金 - - 301,187,375.65 4.33
合计 7,033,593,922.13 100.00 6,952,029,816.67 100.00
6.2报告期末理财产品持有的前十项资产（穿透前）
序号 资产名称 持有金额（元） 占总资产的比例（%）
1 华润信托多资产固收8号集合资金信托计划 348,930,599.13 5.73
"""


NANYIN_SAMPLE = """
南银理财鼎瑞悦稳（最低持有91天）3号公募人民币理财产品2026年第1季度报告
§4  投资组合报告
4.1 报告期末产品资产组合情况
序号 资产类别 穿透前占总资产比例 穿透后占总资产比例
1 固定收益类 95.51% 92.66%
2 权益类 4.49% 7.34%
3 商品及金融衍生品类 0.00% 0.00%
4 混合类 0.00% 0.00%
5 合计 100.00% 100.00%
4.2 报告期末按公允价值占产品资产净值比例大小排序的前十名资产投资明细
序号 代码 名称 公允价值（元） 占产品资产净值比例（％）
1 ZJQTT202306010001 中粮信托-鼎兴2号集合资金信托计划 1,891,457,628.39 18.72
"""


def test_parse_citic_holding_profile_asset_mix():
    product = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")

    profile = parse_holding_profile_from_text(
        product,
        CITIC_SAMPLE,
        report_title="信银理财慧盈象固收增强一年持有期5号理财产品(产品代码：AF233276)2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/citic.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
    )

    rows = {row.asset_class: row for row in profile.asset_mix}
    assert profile.report_period == "2026Q1"
    assert rows["债券"].after_pct == Decimal("59.40")
    assert rows["权益类投资"].after_pct == Decimal("9.96")
    assert rows["现金及银行存款"].after_pct == Decimal("23.87")
    assert profile.top_assets[0].name == "华润信托多资产固收8号集合资金信托计划"


def test_parse_nanyin_holding_profile_asset_mix():
    product = NavProduct("nanyin_wealth", "A32069", "南银理财悦稳最低持有91天3号-B份额")

    profile = parse_holding_profile_from_text(
        product,
        NANYIN_SAMPLE,
        report_title="南银理财鼎瑞悦稳（最低持有91天）3号公募人民币理财产品2026年第1季度报告",
        disclosure_date=date(2026, 4, 22),
        source_url="https://example.test/nanyin.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
    )

    rows = {row.asset_class: row for row in profile.asset_mix}
    assert profile.report_period == "2026Q1"
    assert rows["固定收益类"].after_pct == Decimal("92.66")
    assert rows["权益类"].after_pct == Decimal("7.34")
    assert profile.top_assets[0].code == "ZJQTT202306010001"


def test_target_quarter_waits_for_previous_completed_quarter():
    assert target_quarter_for_check(date(2026, 7, 1)) == "2026Q2"
    assert target_quarter_for_check(date(2026, 1, 5)) == "2025Q4"


def test_estimate_report_uses_market_moves_and_cached_profiles(tmp_path):
    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(
            ("债券", Decimal("13.43"), Decimal("59.40")),
            ("权益类投资", Decimal("0"), Decimal("9.96")),
            ("现金及银行存款", Decimal("9.23"), Decimal("23.87")),
        ),
        top_assets=(),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )
    snapshot = MarketSnapshot(
        trade_date=date(2026, 7, 9),
        symbols=(
            MarketSymbol("沪深300", "000300", Decimal("2.50")),
            MarketSymbol("中证500", "399905", Decimal("3.00")),
            MarketSymbol("十年国债ETF", "511260", Decimal("0.02")),
            MarketSymbol("国债ETF", "511010", Decimal("0.01")),
            MarketSymbol("银华日利", "511880", Decimal("0.00")),
        ),
    )

    report = build_estimate_report(
        cfg,
        snapshot=snapshot,
        cache_path=cache_path,
        generated_at=datetime(2026, 7, 9, 17, 35),
    )

    assert "理财涨跌方向参考" in report
    assert "总体判断：偏平" in report
    assert "慧盈象固收增强一年持有期5号B（AF233276B）" in report
    assert "权益约9.96%，上涨贡献有限" in report
    assert "固收/债券约59.40%" in report


def test_bond_dominant_product_does_not_overreact_to_equity_drop(tmp_path):
    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(
            ("债券", Decimal("13.43"), Decimal("61.50")),
            ("权益类投资", Decimal("0"), Decimal("9.96")),
            ("现金及银行存款", Decimal("9.23"), Decimal("23.87")),
        ),
        top_assets=(),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )
    snapshot = MarketSnapshot(
        trade_date=date(2026, 7, 2),
        symbols=(
            MarketSymbol("沪深300", "000300", Decimal("-2.96")),
            MarketSymbol("中证500", "399905", Decimal("-3.71")),
            MarketSymbol("创业板指", "399006", Decimal("-5.71")),
            MarketSymbol("十年国债ETF", "511260", Decimal("0.08")),
            MarketSymbol("国债ETF", "511010", Decimal("0.04")),
            MarketSymbol("银华日利", "511880", Decimal("0.02")),
        ),
    )

    report = build_estimate_report(
        cfg,
        snapshot=snapshot,
        cache_path=cache_path,
        generated_at=datetime(2026, 7, 9, 17, 35),
    )

    assert "总体判断：偏平" in report
    assert "判断：<font color=\"comment\">偏平</font>" in report
    assert "权益约9.96%，下跌拖累有限" in report
    assert "固收/债券约61.50%，债券偏强" in report


def test_estimate_report_uses_real_top_asset_quotes_when_available(tmp_path, monkeypatch):
    import src.nav_holdings as nav_holdings

    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(
            ("债券", Decimal("13.43"), Decimal("61.50")),
            ("权益类投资", Decimal("0"), Decimal("9.96")),
            ("现金及银行存款", Decimal("9.23"), Decimal("23.87")),
        ),
        top_assets=(TopAsset(name="东方盛虹", code="000301", pct=Decimal("2.71")),),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )
    snapshot = MarketSnapshot(
        trade_date=date(2026, 7, 2),
        symbols=(
            MarketSymbol("沪深300", "000300", Decimal("-2.96")),
            MarketSymbol("中证500", "399905", Decimal("-3.71")),
            MarketSymbol("创业板指", "399006", Decimal("-5.71")),
            MarketSymbol("十年国债ETF", "511260", Decimal("0.08")),
            MarketSymbol("国债ETF", "511010", Decimal("0.04")),
            MarketSymbol("银华日利", "511880", Decimal("0.02")),
        ),
    )

    monkeypatch.setattr(
        nav_holdings,
        "_fetch_top_asset_market_moves",
        lambda top_assets, trade_date: (
            SimpleNamespace(
                name="东方盛虹",
                pct=Decimal("2.71"),
                category="equity",
                change_pct=Decimal("5.00"),
                symbol="sz000301",
            ),
        ),
        raising=False,
    )

    report = nav_holdings.build_estimate_report(
        cfg,
        snapshot=snapshot,
        cache_path=cache_path,
        generated_at=datetime(2026, 7, 9, 17, 35),
    )

    assert "前十资产真实行情：命中1/1，覆盖2.71%" in report
    assert "东方盛虹+5.00%" in report
    assert "判断：<font color=\"warning\">预计上涨</font>" in report


def test_estimate_report_uses_top_assets_to_refine_public_fund_exposure(tmp_path):
    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF247389H",
        name="慧盈象FOF固收增强六个月持有期1号H",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(
            ("债券", Decimal("0"), Decimal("46.61")),
            ("公募基金", Decimal("0"), Decimal("27.74")),
        ),
        top_assets=(
            TopAsset(name="富国纯债E", pct=Decimal("1.61")),
            TopAsset(name="短融ETF海富通", pct=Decimal("1.54")),
            TopAsset(name="博时标普500ETF联接E", pct=Decimal("1.37")),
            TopAsset(name="博时黄金ETF联接E", pct=Decimal("1.25")),
        ),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF247389H",
                        "name": "慧盈象FOF固收增强六个月持有期1号H",
                    }
                ]
            }
        }
    )
    snapshot = MarketSnapshot(
        trade_date=date(2026, 7, 9),
        symbols=(
            MarketSymbol("沪深300", "000300", Decimal("2.50")),
            MarketSymbol("中证500", "399905", Decimal("3.00")),
            MarketSymbol("十年国债ETF", "511260", Decimal("0.02")),
            MarketSymbol("国债ETF", "511010", Decimal("0.01")),
        ),
    )

    report = build_estimate_report(
        cfg,
        snapshot=snapshot,
        cache_path=cache_path,
        generated_at=datetime(2026, 7, 9, 17, 35),
    )

    assert "前十资产识别" in report
    assert "权益约1.37%" in report
    assert "固收/债券约49.76%" in report
    assert "权益约27.74%" not in report


def test_estimate_exposures_prefers_lookthrough_after_pct():
    import src.nav_holdings as nav_holdings

    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(
            ("债券", Decimal("13.43"), Decimal("59.40")),
            ("现金及银行存款", Decimal("9.23"), Decimal("23.87")),
            ("权益类投资", Decimal("0"), Decimal("9.96")),
            ("资产管理产品", Decimal("77.34"), None),
            ("公募基金", Decimal("0"), Decimal("4.33")),
        ),
        top_assets=(),
        status="ok",
        error="",
    )

    exposures, _ = nav_holdings._estimate_exposures(profile)

    assert exposures["bond"] == Decimal("0.594")
    assert exposures["cash"] == Decimal("0.2387")
    assert exposures["equity"] == Decimal("0.0996")
    assert exposures["unknown"] == Decimal("0.0433")


def test_estimate_report_handles_market_fetch_failure(tmp_path, monkeypatch):
    import src.nav_holdings as nav_holdings

    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(("债券", Decimal("0"), Decimal("80")),),
        top_assets=(),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )

    monkeypatch.setattr(nav_holdings, "fetch_market_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("market down")))

    report = nav_holdings.build_estimate_report(cfg, cache_path=cache_path)

    assert "总体判断：无法预估" in report
    assert "行情获取失败" in report


def test_estimate_report_can_use_target_market_date(tmp_path, monkeypatch):
    import src.nav_holdings as nav_holdings

    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q1",
        report_title="2026年1季度运行公告",
        disclosure_date=date(2026, 4, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(("权益类投资", Decimal("0"), Decimal("10")),),
        top_assets=(),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )
    target = date(2026, 7, 8)
    calls = []

    def fake_fetch_market_snapshot(trade_date=None):
        calls.append(trade_date)
        return MarketSnapshot(
            trade_date=target,
            symbols=(MarketSymbol("沪深300", "000300", Decimal("1.20")),),
        )

    monkeypatch.setattr(nav_holdings, "fetch_market_snapshot", fake_fetch_market_snapshot)

    report = nav_holdings.build_estimate_report(cfg, cache_path=cache_path, market_date=target)

    assert calls == [target]
    assert "行情日期：2026-07-08" in report


def test_fetch_market_snapshot_uses_history_for_target_date(monkeypatch):
    import src.nav_holdings as nav_holdings

    target = date(2026, 7, 8)
    expected = MarketSnapshot(
        trade_date=target,
        symbols=(MarketSymbol("沪深300", "000300", Decimal("1.20")),),
    )
    monkeypatch.setattr(nav_holdings, "_fetch_tencent_history_snapshot", lambda trade_date: expected)
    monkeypatch.setattr(
        nav_holdings,
        "_fetch_eastmoney_history_snapshot",
        lambda trade_date: (_ for _ in ()).throw(AssertionError("should use Tencent history first")),
    )

    snapshot = nav_holdings.fetch_market_snapshot(target)

    assert snapshot is expected


def test_fetch_market_snapshot_falls_back_to_eastmoney_history(monkeypatch):
    import src.nav_holdings as nav_holdings

    target = date(2026, 7, 8)
    expected = MarketSnapshot(
        trade_date=target,
        symbols=(MarketSymbol("沪深300", "000300", Decimal("1.20")),),
    )
    monkeypatch.setattr(
        nav_holdings,
        "_fetch_tencent_history_snapshot",
        lambda trade_date: (_ for _ in ()).throw(RuntimeError("tencent down")),
    )
    monkeypatch.setattr(nav_holdings, "_fetch_eastmoney_history_snapshot", lambda trade_date: expected)

    snapshot = nav_holdings.fetch_market_snapshot(target)

    assert snapshot is expected


def test_quarterly_refresh_skips_cached_current_period(tmp_path, monkeypatch):
    import src.nav_holdings as nav_holdings

    profile = HoldingProfile(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
        report_period="2026Q2",
        report_title="2026年2季度运行公告",
        disclosure_date=date(2026, 7, 21),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 21, 17, 30),
        asset_mix=(("债券", Decimal("0"), Decimal("80")),),
        top_assets=(),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)
    cfg = Config(
        {
            "nav_monitor": {
                "products": [
                    {
                        "provider": "citic_wealth",
                        "code": "AF233276B",
                        "name": "慧盈象固收增强一年持有期5号B",
                    }
                ]
            }
        }
    )

    monkeypatch.setattr(
        nav_holdings,
        "refresh_profile_for_product",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not refresh")),
    )

    assert nav_holdings.refresh_quarterly_profiles_if_due(
        cfg,
        today=date(2026, 7, 22),
        cache_path=cache_path,
    ) == []


def test_format_holdings_profiles_and_status(tmp_path):
    profile = HoldingProfile(
        provider="nanyin_wealth",
        code="A32069",
        name="南银理财悦稳最低持有91天3号-B份额",
        report_period="2026Q1",
        report_title="2026年第1季度报告",
        disclosure_date=date(2026, 4, 22),
        source_url="https://example.test/report.pdf",
        updated_at=datetime(2026, 7, 9, 17, 30),
        asset_mix=(("固定收益类", Decimal("95.51"), Decimal("92.66")),),
        top_assets=tuple(
            TopAsset(name=f"资产{index}", amount=Decimal("10000"), pct=Decimal(str(index)))
            for index in range(1, 11)
        ),
        status="ok",
        error="",
    )
    cache_path = tmp_path / "profiles.json"
    update_profile_cache([profile], cache_path=cache_path)

    all_text = format_holdings_profiles(cache_path=cache_path)
    one_text = format_holdings_profiles(code="A32069", cache_path=cache_path)
    status_text = format_holdings_status(cache_path=cache_path)

    assert "持仓画像" in all_text
    assert "固定收益类：92.66%" in one_text
    assert "前十资产（10项）" in one_text
    assert "1. 资产1（1.00%）" in one_text
    assert "10. 资产10（10.00%）" in one_text
    assert "2026Q1" in status_text
