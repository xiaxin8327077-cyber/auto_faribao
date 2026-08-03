import os
from datetime import date, datetime
from decimal import Decimal

import pytest

from src.config import Config, load_config, save_config
from src.nav_monitor import (
    CiticWealthProvider,
    DateQueryResult,
    NanyinWealthProvider,
    NavProduct,
    NavRecord,
    ProductCandidate,
    ProductNavResult,
    build_nav_period_report,
    build_add_product_candidates,
    calculate_change,
    calculate_period_start,
    confirm_pending_nav_add,
    format_product_candidates,
    format_nav_report,
    parse_nav_query_date,
    query_nav_period_products,
    save_pending_nav_add,
    set_nav_product_shares_batch,
    set_nav_product_shares,
)


def test_public_fund_market_quote_converts_to_legacy_nav_record():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    product = MarketProduct(
        "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND
    )
    quote = MarketQuote(
        "003103", date(2026, 7, 29), "changsheng_fund", "hash",
        unit_nav=Decimal("1.0321"),
        cumulative_nav=Decimal("1.1288"),
    )

    record = market_quote_to_nav_record(product, quote)

    assert record.provider == "changsheng_fund"
    assert record.code == "003103"
    assert record.name == "长盛盛裕纯债C"
    assert record.nav_date == date(2026, 7, 29)
    assert record.unit_nav == Decimal("1.0321")
    assert record.cumulative_nav == Decimal("1.1288")
    assert record.source == "changsheng_fund"


def test_wealth_nav_market_quote_converts_to_legacy_nav_record():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    product = MarketProduct(
        "citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B", ProductType.WEALTH_NAV
    )
    quote = MarketQuote(
        "AF233276B", date(2026, 7, 29), "citic_wealth", "hash",
        unit_nav=Decimal("1.077800"),
    )

    record = market_quote_to_nav_record(product, quote)

    assert record.nav_date == date(2026, 7, 29)
    assert record.unit_nav == Decimal("1.077800")
    assert record.cumulative_nav is None


def test_cash_market_quote_cannot_be_forced_into_nav_record():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    with pytest.raises(ValueError, match="^cash management quote has no unit NAV$"):
        market_quote_to_nav_record(
            MarketProduct("nanyin_wealth", "NYRR000007", "日日聚宝", ProductType.CASH_MANAGEMENT),
            MarketQuote(
                "NYRR000007", date(2026, 7, 30), "nanyin_wealth", "hash",
                income_per_10k=Decimal("0.4475"),
            ),
        )


def test_market_quote_code_must_match_product_before_conversion():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    product = MarketProduct(
        "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND
    )
    quote = MarketQuote(
        "015736", date(2026, 7, 29), "changsheng_fund", "hash",
        unit_nav=Decimal("1.0321"),
    )

    with pytest.raises(ValueError, match="market quote does not match product code"):
        market_quote_to_nav_record(product, quote)


def test_nav_market_quote_requires_unit_nav_for_conversion():
    from src.portfolio_models import MarketProduct, MarketQuote, ProductType
    from src.nav_monitor import market_quote_to_nav_record

    product = MarketProduct(
        "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND
    )
    quote = MarketQuote("003103", date(2026, 7, 29), "changsheng_fund", "hash")

    with pytest.raises(ValueError, match="market quote is missing unit NAV"):
        market_quote_to_nav_record(product, quote)


def _sample_nav_results_for_image():
    p1 = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")
    p2 = NavProduct("nanyin_wealth", "A32069", "南银理财悦稳最低持有91天3号-B份额")
    return [
        ProductNavResult(
            product=p1,
            latest=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 8), Decimal("1.077800")),
            previous=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 7), Decimal("1.078000")),
            shares=Decimal("401133.95"),
        ),
        ProductNavResult(
            product=p2,
            latest=NavRecord("nanyin_wealth", "A32069", p2.name, date(2026, 7, 8), Decimal("1.050881")),
            previous=NavRecord("nanyin_wealth", "A32069", p2.name, date(2026, 7, 7), Decimal("1.050853")),
            shares=Decimal("101423.76"),
        ),
    ]


def _sample_period_nav_results_for_image():
    from src.nav_monitor import ProductPeriodNavResult

    p1 = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")
    p2 = NavProduct("nanyin_wealth", "A32069", "南银理财悦稳最低持有91天3号-B份额")
    return [
        ProductPeriodNavResult(
            product=p1,
            latest=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 8), Decimal("1.078000")),
            baseline=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 1), Decimal("1.076700")),
            shares=Decimal("10000"),
        ),
        ProductPeriodNavResult(
            product=p2,
            latest=NavRecord("nanyin_wealth", "A32069", p2.name, date(2026, 7, 8), Decimal("1.050881")),
            baseline=NavRecord("nanyin_wealth", "A32069", p2.name, date(2025, 12, 31), Decimal("1.041491")),
            shares=Decimal("10000"),
        ),
    ]


def test_nav_report_image_model_places_total_next_to_title():
    from src.nav_report_image import build_nav_report_image_model

    model = build_nav_report_image_model(
        _sample_nav_results_for_image(),
        title="理财净值日报",
        generated_at=datetime(2026, 7, 9, 10, 55),
    )

    assert model.title == "理财净值日报"
    assert model.query_line == "查询时间：2026-07-09 10:55    最新披露净值 vs 上一期披露净值"
    assert model.total_amount == Decimal("-77.39")
    assert model.weather == "rain"
    assert model.rows[0].code == "AF233276B"
    assert model.rows[0].nav_text == "1.077800"
    assert model.rows[0].nav_date_text == "2026-07-08"
    assert model.rows[0].change_text == "-0.000200"
    assert model.rows[0].income_text == "-80.23 元"
    assert model.rows[1].change_text == "+0.000028"
    assert model.rows[1].income_text == "2.84 元"


def test_nav_period_report_image_model_uses_period_columns():
    from src.nav_report_image import build_nav_period_report_image_model

    model = build_nav_period_report_image_model(
        _sample_period_nav_results_for_image(),
        title="理财净值月报",
        period_label="月度",
        start_date=date(2026, 7, 1),
        generated_at=datetime(2026, 7, 9, 10, 55),
    )

    assert model.title == "理财净值月报"
    assert model.query_line == "查询时间：2026-07-09 10:55    统计周期：月度    周期起点：2026-07-01"
    assert model.total_amount == Decimal("106.90")
    assert model.weather == "sun"
    assert model.rows[0].start_nav_text == "1.076700"
    assert model.rows[0].start_date_text == "2026-07-01"
    assert model.rows[0].end_nav_text == "1.078000"
    assert model.rows[0].end_date_text == "2026-07-08"
    assert model.rows[0].change_text == "+0.001300"
    assert model.rows[0].income_text == "13.00 元"


def test_render_nav_period_report_image_creates_png(tmp_path):
    from PIL import Image
    from src.nav_report_image import render_nav_period_report_image

    image_path = render_nav_period_report_image(
        _sample_period_nav_results_for_image(),
        title="理财净值月报",
        period_label="月度",
        start_date=date(2026, 7, 1),
        generated_at=datetime(2026, 7, 9, 10, 55),
        output_dir=tmp_path,
    )

    assert image_path.endswith(".png")
    with Image.open(image_path) as image:
        assert image.size[0] == 1320
        assert image.size[1] < 500


def test_render_nav_report_image_creates_compact_png(tmp_path):
    from PIL import Image
    from src.nav_report_image import render_nav_report_image

    image_path = render_nav_report_image(
        _sample_nav_results_for_image(),
        title="理财净值日报",
        generated_at=datetime(2026, 7, 9, 10, 55),
        output_dir=tmp_path,
    )

    assert image_path.endswith(".png")
    assert os.path.getsize(image_path) < 2 * 1024 * 1024
    with Image.open(image_path) as image:
        assert image.size[0] == 1320
        assert image.size[1] < 500
        assert image.getbbox() is not None


def test_nav_report_image_total_uses_unrounded_product_amounts():
    from src.nav_report_image import build_nav_report_image_model

    products = [
        ProductNavResult(
            product=NavProduct("citic_wealth", "P1", "产品1"),
            latest=NavRecord("citic_wealth", "P1", "产品1", date(2026, 7, 8), Decimal("1.000004")),
            previous=NavRecord("citic_wealth", "P1", "产品1", date(2026, 7, 7), Decimal("1.000000")),
            shares=Decimal("1000"),
        ),
        ProductNavResult(
            product=NavProduct("citic_wealth", "P2", "产品2"),
            latest=NavRecord("citic_wealth", "P2", "产品2", date(2026, 7, 8), Decimal("1.000004")),
            previous=NavRecord("citic_wealth", "P2", "产品2", date(2026, 7, 7), Decimal("1.000000")),
            shares=Decimal("1000"),
        ),
    ]

    model = build_nav_report_image_model(products, generated_at=datetime(2026, 7, 9, 10, 55))

    assert model.rows[0].income_text == "0.00 元"
    assert model.rows[1].income_text == "0.00 元"
    assert model.total_amount == Decimal("0.01")


def test_push_nav_period_report_prefers_image_message(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "慧盈象固收增强一年持有期5号B"}
                ]
            },
        }
    )
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_period_products",
        lambda cfg, period, base_date=None: (date(2026, 7, 1), _sample_period_nav_results_for_image()),
    )

    sent = []
    monkeypatch.setattr(
        "src.wechat_notifier.send_image",
        lambda wechat, image_path, to_user=None: sent.append(("image", os.path.exists(image_path), to_user)) or True,
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda wechat, content, to_user=None: sent.append(("markdown", content, to_user)) or True,
    )

    report = nav_monitor.push_nav_period_report(
        cfg,
        "month",
        base_date=date(2026, 7, 9),
        to_user="user1",
        image_output_dir=tmp_path,
    )

    assert "理财净值月报" in report
    assert [item[0] for item in sent] == ["image"]
    assert sent[0][1] is True
    assert sent[0][2] == "user1"


def test_push_nav_period_report_falls_back_to_markdown_when_image_send_fails(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "慧盈象固收增强一年持有期5号B"}
                ]
            },
        }
    )
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_period_products",
        lambda cfg, period, base_date=None: (date(2026, 7, 1), _sample_period_nav_results_for_image()),
    )

    sent = []
    monkeypatch.setattr(
        "src.wechat_notifier.send_image",
        lambda wechat, image_path, to_user=None: sent.append(("image", to_user)) or False,
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda wechat, content, to_user=None: sent.append(("markdown", to_user, content)) or True,
    )

    nav_monitor.push_nav_period_report(
        cfg,
        "month",
        base_date=date(2026, 7, 9),
        to_user="user1",
        image_output_dir=tmp_path,
    )

    assert [item[0] for item in sent] == ["image", "markdown"]
    assert "理财净值月报" in sent[1][2]


def test_push_nav_report_prefers_image_message(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "慧盈象固收增强一年持有期5号B"}
                ]
            },
        }
    )
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_products",
        lambda cfg, target_date=None: _sample_nav_results_for_image(),
    )

    sent = []

    def fake_send_image(wechat, image_path, to_user=None):
        sent.append(("image", image_path, os.path.exists(image_path), to_user))
        return True

    def fake_send_markdown(wechat, content, to_user=None):
        sent.append(("markdown", content, to_user))
        return True

    monkeypatch.setattr("src.wechat_notifier.send_image", fake_send_image)
    monkeypatch.setattr("src.wechat_notifier.send_markdown", fake_send_markdown)

    report = nav_monitor.push_nav_report(cfg, to_user="user1", image_output_dir=tmp_path)

    assert report.startswith("##")
    assert [item[0] for item in sent] == ["image"]
    assert sent[0][2] is True
    assert sent[0][3] == "user1"
    assert not os.path.exists(sent[0][1])


def test_push_nav_report_falls_back_to_markdown_when_image_send_fails(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "慧盈象固收增强一年持有期5号B"}
                ]
            },
        }
    )
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_products",
        lambda cfg, target_date=None: _sample_nav_results_for_image(),
    )

    sent = []
    monkeypatch.setattr(
        "src.wechat_notifier.send_image",
        lambda wechat, image_path, to_user=None: sent.append(("image", to_user)) or False,
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda wechat, content, to_user=None: sent.append(("markdown", to_user, content)) or True,
    )

    nav_monitor.push_nav_report(cfg, to_user="user1", image_output_dir=tmp_path)

    assert [item[0] for item in sent] == ["image", "markdown"]
    assert sent[1][1] == "user1"
    assert "理财净值日报" in sent[1][2]


def test_push_nav_evening_report_sends_only_today_disclosed_products(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "今日产品"},
                    {"provider": "citic_wealth", "code": "AF233262B", "name": "昨日产品"},
                ]
            },
        }
    )
    today_product = NavProduct("citic_wealth", "AF233276B", "今日产品")
    old_product = NavProduct("citic_wealth", "AF233262B", "昨日产品")
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_products",
        lambda cfg, target_date=None: [
            ProductNavResult(
                product=today_product,
                latest=NavRecord("citic_wealth", "AF233276B", "今日产品", date(2026, 7, 9), Decimal("1.100000")),
                previous=NavRecord("citic_wealth", "AF233276B", "今日产品", date(2026, 7, 8), Decimal("1.090000")),
            ),
            ProductNavResult(
                product=old_product,
                latest=NavRecord("citic_wealth", "AF233262B", "昨日产品", date(2026, 7, 8), Decimal("1.050000")),
                previous=NavRecord("citic_wealth", "AF233262B", "昨日产品", date(2026, 7, 7), Decimal("1.040000")),
            ),
        ],
    )
    sent = []
    monkeypatch.setattr(
        "src.wechat_notifier.send_image",
        lambda wechat, image_path, to_user=None: sent.append(("image", os.path.exists(image_path), to_user)) or True,
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda wechat, content, to_user=None: sent.append(("markdown", content, to_user)) or True,
    )

    report = nav_monitor.push_nav_evening_report(
        cfg,
        as_of_date=date(2026, 7, 9),
        to_user="user1",
        image_output_dir=tmp_path,
    )

    assert "理财净值晚报" in report
    assert "今日产品" in report
    assert "昨日产品" not in report
    assert [item[0] for item in sent] == ["image"]
    assert sent[0][1] is True
    assert sent[0][2] == "user1"


def test_push_nav_evening_report_skips_when_no_today_disclosed_products(monkeypatch, tmp_path):
    import src.nav_monitor as nav_monitor

    cfg = Config(
        {
            "wechat": {"corpid": "c", "corpsecret": "s", "agentid": 1, "to_user": "all"},
            "nav_monitor": {
                "products": [
                    {"provider": "citic_wealth", "code": "AF233276B", "name": "昨日产品"},
                ]
            },
        }
    )
    product = NavProduct("citic_wealth", "AF233276B", "昨日产品")
    monkeypatch.setattr(
        nav_monitor,
        "query_nav_products",
        lambda cfg, target_date=None: [
            ProductNavResult(
                product=product,
                latest=NavRecord("citic_wealth", "AF233276B", "昨日产品", date(2026, 7, 8), Decimal("1.100000")),
            )
        ],
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not send image")),
    )
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not send markdown")),
    )

    report = nav_monitor.push_nav_evening_report(
        cfg,
        as_of_date=date(2026, 7, 9),
        image_output_dir=tmp_path,
    )

    assert report == ""


def test_nav_monitor_config_defaults_to_enabled_with_0800():
    cfg = Config({})

    assert cfg.nav_monitor.enabled is True
    assert cfg.nav_monitor.push_hour == 8
    assert cfg.nav_monitor.push_minute == 0
    assert cfg.nav_monitor.evening_push_enabled is True
    assert cfg.nav_monitor.evening_push_hour == 23
    assert cfg.nav_monitor.evening_push_minute == 30
    assert cfg.nav_monitor.products == []


def test_config_defaults_keep_readable_chinese_text():
    cfg = Config({})

    assert cfg.source.name_field == "任务名称"
    assert cfg.source.person_names == ["刘非凡"]
    assert "这是4位数字验证码" in cfg.captcha.prompt


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
                    "shares": Decimal("10000.5"),
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
    assert data["nav_monitor"]["evening_push_enabled"] is True
    assert data["nav_monitor"]["evening_push_hour"] == 23
    assert data["nav_monitor"]["evening_push_minute"] == 30
    assert data["nav_monitor"]["products"][0]["provider"] == "citic_wealth"
    assert data["nav_monitor"]["products"][0]["code"] == "AF233276B"
    assert data["nav_monitor"]["products"][0]["shares"] == 10000.5


def test_load_config_reads_utf8_nav_product_names(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
nav_monitor:
  products:
  - provider: citic_wealth
    code: AF233276B
    name: 慧盈象固收增强一年持有期5号B
""",
        encoding="utf-8",
    )

    cfg = load_config(str(path))

    assert cfg.nav_monitor.products[0].name == "慧盈象固收增强一年持有期5号B"


def test_parse_relative_dates():
    base = date(2026, 7, 8)

    assert parse_nav_query_date("查询昨天净值", base) == date(2026, 7, 7)
    assert parse_nav_query_date("前天净值", base) == date(2026, 7, 6)
    assert parse_nav_query_date("今天净值", base) == date(2026, 7, 8)


def test_parse_compact_query_date():
    assert parse_nav_query_date("查询净值 20260707", date(2026, 7, 8)) == date(2026, 7, 7)


def test_calculate_period_start_uses_natural_periods():
    base = date(2026, 7, 9)

    assert calculate_period_start("week", base) == date(2026, 7, 6)
    assert calculate_period_start("month", base) == date(2026, 7, 1)
    assert calculate_period_start("quarter", base) == date(2026, 7, 1)
    assert calculate_period_start("half_year", base) == date(2026, 7, 1)
    assert calculate_period_start("year", base) == date(2026, 1, 1)


def test_calculate_period_start_supports_rolling_day_periods():
    base = date(2026, 7, 9)

    assert calculate_period_start("rolling_7d", base) == date(2026, 7, 2)
    assert calculate_period_start("rolling_1m", base) == date(2026, 6, 9)
    assert calculate_period_start("rolling_3m", base) == date(2026, 4, 10)
    assert calculate_period_start("rolling_6m", base) == date(2026, 1, 10)
    assert calculate_period_start("rolling_1y", base) == date(2025, 7, 9)
    assert calculate_period_start("rolling_2y", base) == date(2024, 7, 9)
    assert calculate_period_start("rolling_3y", base) == date(2023, 7, 10)


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
    assert "净值：1.078000（2026-07-07）" in text
    assert "涨跌：<font color=\"info\">-0.000100（-0.0093%）</font>" in text


def test_latest_report_uses_daily_report_markdown_style():
    product = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")
    latest = NavRecord(
        provider="citic_wealth",
        code="AF233276B",
        name=product.name,
        nav_date=date(2026, 7, 7),
        unit_nav=Decimal("1.078000"),
        cumulative_nav=Decimal("1.078000"),
        source="test",
    )
    previous = NavRecord(
        provider="citic_wealth",
        code="AF233276B",
        name=product.name,
        nav_date=date(2026, 7, 6),
        unit_nav=Decimal("1.078100"),
        cumulative_nav=Decimal("1.078100"),
        source="test",
    )

    text = format_nav_report(
        [ProductNavResult(product=product, latest=latest, previous=previous)],
        title="理财净值日报",
        generated_at=datetime(2026, 7, 8, 8, 0),
    )

    assert text.startswith("## 📈 理财净值日报")
    assert "> **查询时间**：2026-07-08 08:00" in text
    assert "> **产品数量**" not in text
    assert "> **计算口径**" not in text
    assert "> **1. 慧盈象固收增强一年持有期5号B（AF233276B）**" in text
    assert "**机构**" not in text
    assert "**产品代码**" not in text
    assert "**最新净值**" not in text
    assert "**上期净值**" not in text
    assert "**涨跌幅**" not in text
    assert "净值：1.078000（2026-07-07）" in text
    assert "涨跌：<font color=\"info\">-0.000100（-0.0093%）</font>" in text
    assert "｜" not in text


def test_latest_report_includes_estimated_total_and_product_profit():
    p1 = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")
    p2 = NavProduct("citic_wealth", "AF233262B", "慧盈象固收增强六个月持有期1号B")
    text = format_nav_report(
        [
            ProductNavResult(
                product=p1,
                latest=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 7), Decimal("1.0780")),
                previous=NavRecord("citic_wealth", "AF233276B", p1.name, date(2026, 7, 6), Decimal("1.0781")),
                shares=Decimal("10000"),
            ),
            ProductNavResult(
                product=p2,
                latest=NavRecord("citic_wealth", "AF233262B", p2.name, date(2026, 7, 7), Decimal("1.0708")),
                previous=NavRecord("citic_wealth", "AF233262B", p2.name, date(2026, 7, 6), Decimal("1.0703")),
                shares=Decimal("20000"),
            ),
        ],
        title="理财净值日报",
        generated_at=datetime(2026, 7, 8, 8, 0),
    )

    assert "> **预估总收益**：<font color=\"warning\">9.00 元</font>" in text
    assert "**持仓份额**" not in text
    assert "**预估收益**" not in text
    assert "> **1. 慧盈象固收增强一年持有期5号B（AF233276B）**" in text
    assert "份额：10000　净值：1.078000（2026-07-07）" in text
    assert "涨跌：<font color=\"info\">-0.000100（-0.0093%）</font>　收益：<font color=\"info\">-1.00 元</font>" in text
    assert "> **2. 慧盈象固收增强六个月持有期1号B（AF233262B）**" in text
    assert "份额：20000　净值：1.070800（2026-07-07）" in text
    assert "涨跌：<font color=\"warning\">+0.000500（+0.0467%）</font>　收益：<font color=\"warning\">10.00 元</font>" in text
    assert "｜" not in text
    assert "`" not in text


def test_latest_report_pads_compact_columns_for_alignment():
    p1 = NavProduct("citic_wealth", "P1", "产品1")
    p2 = NavProduct("citic_wealth", "P2", "产品2")
    text = format_nav_report(
        [
            ProductNavResult(
                product=p1,
                latest=NavRecord("citic_wealth", "P1", p1.name, date(2026, 7, 7), Decimal("1.0001")),
                previous=NavRecord("citic_wealth", "P1", p1.name, date(2026, 7, 6), Decimal("1.0000")),
                shares=Decimal("1"),
            ),
            ProductNavResult(
                product=p2,
                latest=NavRecord("citic_wealth", "P2", p2.name, date(2026, 7, 7), Decimal("1.0001")),
                previous=NavRecord("citic_wealth", "P2", p2.name, date(2026, 7, 6), Decimal("1.0000")),
                shares=Decimal("20000"),
            ),
        ],
        title="理财净值日报",
        generated_at=datetime(2026, 7, 8, 8, 0),
    )

    assert "份额：1　净值：1.000100（2026-07-07）" in text
    assert "份额：20000　净值：1.000100（2026-07-07）" in text
    assert "｜" not in text
    assert "`" not in text


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

    assert "> **查询日期**：2026-07-02" in text
    assert "> **产品数量**" not in text
    assert "### 南银理财致远一年定开19期A份额（NYZY000022）" in text
    assert "**产品代码**" not in text
    assert "**状态**：该日未披露" in text
    assert "**最近披露**：2026-06-26  1.000000" in text


def test_period_report_includes_return_and_amount(monkeypatch):
    records = [
        NavRecord(
            provider="citic_wealth",
            code="AF233276B",
            name="慧盈象固收增强一年持有期5号B",
            nav_date=date(2026, 7, 7),
            unit_nav=Decimal("1.0780"),
            cumulative_nav=None,
            source="test",
        ),
        NavRecord(
            provider="citic_wealth",
            code="AF233276B",
            name="慧盈象固收增强一年持有期5号B",
            nav_date=date(2026, 7, 1),
            unit_nav=Decimal("1.0767"),
            cumulative_nav=None,
            source="test",
        ),
    ]

    class FakeProvider:
        def fetch_latest(self, product, **kwargs):
            return records

    monkeypatch.setattr("src.nav_monitor.get_provider", lambda provider: FakeProvider())
    cfg = Config({
        "nav_monitor": {
            "products": [
                {
                    "provider": "citic_wealth",
                    "code": "AF233276B",
                    "name": "慧盈象固收增强一年持有期5号B",
                    "shares": 10000,
                }
            ]
        }
    })

    text = build_nav_period_report(
        cfg,
        "month",
        base_date=date(2026, 7, 9),
        generated_at=datetime(2026, 7, 9, 9, 30),
    )

    assert "## 📊 理财净值月报" in text
    assert "> **统计周期**：月度" in text
    assert "> **周期起点**：2026-07-01" in text
    assert "> **产品数量**" not in text
    assert "**机构**" not in text
    assert "> **1. 慧盈象固收增强一年持有期5号B（AF233276B）**" in text
    assert "**产品代码**" not in text
    assert "**期初净值**" not in text
    assert "**净值变动**" not in text
    assert "**持仓份额**" not in text
    assert "**估算收益**" not in text
    assert "份额：10000　期初：1.076700（2026-07-01）　期末：1.078000（2026-07-07）" in text
    assert "涨跌：<font color=\"warning\">+0.001300（+0.1207%）</font>　收益：<font color=\"warning\">13.00 元</font>" in text
    assert "｜" not in text
    assert "`" not in text


def test_natural_month_query_includes_first_day_profit(monkeypatch):
    product_name = "慧盈象固收增强一年持有期5号B"

    class FakeProvider:
        def fetch_latest(self, product, **kwargs):
            return [
                NavRecord("citic_wealth", "AF233276B", product_name, date(2026, 7, 10), Decimal("1.0780")),
                NavRecord("citic_wealth", "AF233276B", product_name, date(2026, 7, 1), Decimal("1.0785")),
                NavRecord("citic_wealth", "AF233276B", product_name, date(2026, 6, 30), Decimal("1.0784")),
            ]

    monkeypatch.setattr("src.nav_monitor.get_provider", lambda provider: FakeProvider())
    cfg = Config({
        "nav_monitor": {
            "products": [
                {
                    "provider": "citic_wealth",
                    "code": "AF233276B",
                    "name": product_name,
                    "shares": 401133.95,
                }
            ]
        }
    })

    start_date, results = query_nav_period_products(
        cfg,
        "month",
        base_date=date(2026, 7, 13),
    )

    assert start_date == date(2026, 7, 1)
    assert results[0].baseline is not None
    assert results[0].baseline.nav_date == date(2026, 6, 30)


def test_period_report_uses_natural_period_titles():
    cfg = Config({"nav_monitor": {"products": []}})

    assert build_nav_period_report(cfg, "week").startswith("## 📊 理财净值周报")
    assert build_nav_period_report(cfg, "month").startswith("## 📊 理财净值月报")
    assert build_nav_period_report(cfg, "quarter").startswith("## 📊 理财净值季报")
    assert build_nav_period_report(cfg, "half_year").startswith("## 📊 理财净值半年报")
    assert build_nav_period_report(cfg, "year").startswith("## 📊 理财净值年报")
    assert build_nav_period_report(cfg, "rolling_7d").startswith("## 📊 理财净值统计")


def test_period_report_fetches_before_natural_start_for_baseline(monkeypatch):
    calls = []
    product_name = "南银理财悦稳最低持有91天3号-B份额"

    class FakeProvider:
        def fetch_latest(self, product, **kwargs):
            calls.append(kwargs)
            records = [
                NavRecord("nanyin_wealth", "A32069", product_name, date(2026, 7, 8), Decimal("1.050881")),
            ]
            if kwargs["start_date"] <= date(2025, 12, 31):
                records.append(
                    NavRecord("nanyin_wealth", "A32069", product_name, date(2025, 12, 31), Decimal("1.041491"))
                )
            return records

    monkeypatch.setattr("src.nav_monitor.get_provider", lambda provider: FakeProvider())
    cfg = Config({
        "nav_monitor": {
            "products": [
                {
                    "provider": "nanyin_wealth",
                    "code": "A32069",
                    "name": product_name,
                    "shares": 101423.76,
                }
            ]
        }
    })

    text = build_nav_period_report(
        cfg,
        "year",
        base_date=date(2026, 7, 9),
        generated_at=datetime(2026, 7, 9, 10, 38),
    )

    assert calls[0]["start_date"] < date(2026, 1, 1)
    assert "> **周期起点**：2026-01-01" in text
    assert "期初：1.041491（2025-12-31）" in text
    assert "期末：1.050881（2026-07-08）" in text
    assert "　收益：<font color=\"warning\">" in text


def test_period_query_uses_first_record_after_start_when_no_earlier_baseline(monkeypatch):
    product_name = "慧盈象固收增强一年持有期5号B"

    class FakeProvider:
        def fetch_latest(self, product, **kwargs):
            return [
                NavRecord("citic_wealth", "AF233276B", product_name, date(2026, 7, 8), Decimal("1.077800")),
                NavRecord("citic_wealth", "AF233276B", product_name, date(2023, 12, 29), Decimal("1.000000")),
            ]

    monkeypatch.setattr("src.nav_monitor.get_provider", lambda provider: FakeProvider())
    cfg = Config({
        "nav_monitor": {
            "products": [
                {
                    "provider": "citic_wealth",
                    "code": "AF233276B",
                    "name": product_name,
                }
            ]
        }
    })

    start_date, results = query_nav_period_products(cfg, "rolling_3y", base_date=date(2026, 7, 9))

    assert start_date == date(2023, 7, 10)
    assert results[0].baseline is not None
    assert results[0].baseline.nav_date == date(2023, 12, 29)


def test_citic_provider_parses_history_and_candidates():
    class FakeCiticClient:
        def get_json(self, path, params):
            if path.endswith("/search"):
                return {
                    "code": "0000",
                    "data": [
                        {
                            "prodCode": "AF233276B",
                            "prodNameShort": "慧盈象固收增强一年持有期5号B",
                            "registCode": "Z7002623000809",
                            "navDate": "20260707",
                            "nav": "1.0780",
                            "totalNav": "1.0780",
                        }
                    ],
                }
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {"prodCode": "AF233276B", "navDate": "20260706", "nav": "1.0781"},
                        {"prodCode": "AF233276B", "navDate": "20260707", "nav": "1.0780"},
                    ]
                },
            }

    provider = CiticWealthProvider(client=FakeCiticClient())
    product = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")

    records = provider.fetch_latest(product)
    candidates = provider.search_products("AF233276B")

    assert [r.nav_date for r in records[:2]] == [date(2026, 7, 7), date(2026, 7, 6)]
    assert records[0].unit_nav == Decimal("1.0780")
    assert candidates[0].code == "AF233276B"
    assert candidates[0].latest_nav_date == date(2026, 7, 7)
    assert candidates[0].latest_unit_nav == Decimal("1.0780")


def test_citic_date_query_expands_history_window_for_older_target():
    calls = []

    class FakeCiticClient:
        def get_json(self, path, params):
            calls.append(params)
            query_unit = params.get("queryUnit")
            if query_unit == 1:
                return {
                    "code": "0000",
                    "data": {
                        "productNavPic": [
                            {"prodCode": "AF233276B", "navDate": "20260608", "nav": "1.0760"},
                        ]
                    },
                }
            return {
                "code": "0000",
                "data": {
                    "productNavPic": [
                        {"prodCode": "AF233276B", "navDate": "20260430", "nav": "1.0737"},
                        {"prodCode": "AF233276B", "navDate": "20260506", "nav": "1.0739"},
                        {"prodCode": "AF233276B", "navDate": "20260708", "nav": "1.0778"},
                    ]
                },
            }

    provider = CiticWealthProvider(client=FakeCiticClient())
    product = NavProduct("citic_wealth", "AF233276B", "慧盈象固收增强一年持有期5号B")

    result = provider.fetch_by_date(product, date(2026, 5, 5))

    assert calls[0]["queryUnit"] > 1
    assert result.exact is False
    assert result.record.nav_date == date(2026, 4, 30)
    assert result.record.unit_nav == Decimal("1.0737")


def test_nanyin_provider_parses_encrypted_payload_results():
    class FakeNanyinClient:
        def post_encrypted_json(self, path, payload):
            if "queryProductDetail" in path:
                return {
                    "salesCode": "NYZY000022",
                    "title": "南银理财致远一年定开19期A份额",
                    "financingRegisterCode": "Z7003226000190",
                }
            return {
                "aaData": [
                    {
                        "productCode": "NYZY000022",
                        "date": "2026-07-03",
                        "netValue": "1.0006",
                        "cumulativeNetValue": "1.0006",
                    },
                    {
                        "productCode": "NYZY000022",
                        "date": "2026-06-26",
                        "netValue": "1.0000",
                        "cumulativeNetValue": "1.0000",
                    },
                ]
            }

    provider = NanyinWealthProvider(client=FakeNanyinClient())
    product = NavProduct("nanyin_wealth", "NYZY000022", "南银理财致远一年定开19期A份额")

    candidate = provider.get_product_identity("NYZY000022")
    records = provider.fetch_latest(product, as_of=date(2026, 7, 8))
    missed = provider.fetch_by_date(product, date(2026, 7, 2))

    assert candidate.code == "NYZY000022"
    assert candidate.register_code == "Z7003226000190"
    assert records[0].nav_date == date(2026, 7, 3)
    assert records[0].unit_nav == Decimal("1.0006")
    assert missed.exact is False
    assert missed.record.nav_date == date(2026, 6, 26)


def test_add_product_candidates_display_latest_nav():
    class FakeProvider:
        def search_products(self, query):
            return [
                ProductCandidate(
                    provider="citic_wealth",
                    code="AF233276B",
                    name="慧盈象固收增强一年持有期5号B",
                    register_code="Z7002623000809",
                    latest_nav_date=date(2026, 7, 7),
                    latest_unit_nav=Decimal("1.0780"),
                    latest_cumulative_nav=Decimal("1.0780"),
                )
            ]

    candidates = build_add_product_candidates(FakeProvider(), "AF233276B")
    text = format_product_candidates(candidates)

    assert "1. 信银理财" in text
    assert "代码：AF233276B" in text
    assert "登记编码：Z7002623000809" in text
    assert "最新净值：2026-07-07  1.078000" in text
    assert "回复：确认添加净值产品 1" in text


def test_confirm_pending_nav_add_writes_only_selected_candidate(tmp_path):
    cfg = Config({"nav_monitor": {"products": []}})
    pending_path = tmp_path / "nav_pending.json"
    candidates = [
        ProductCandidate("citic_wealth", "AF233276B", "P1"),
        ProductCandidate("nanyin_wealth", "NYZY000022", "P2"),
    ]

    save_pending_nav_add(candidates, pending_path=str(pending_path), now=datetime(2026, 7, 8, 8, 0))
    ok, message = confirm_pending_nav_add(
        cfg,
        2,
        pending_path=str(pending_path),
        now=datetime(2026, 7, 8, 8, 1),
    )

    assert ok is True
    assert "已添加" in message
    assert len(cfg.nav_monitor.products) == 1
    assert cfg.nav_monitor.products[0].provider == "nanyin_wealth"
    assert cfg.nav_monitor.products[0].code == "NYZY000022"
    assert not pending_path.exists()


def test_confirm_pending_nav_add_rejects_duplicate(tmp_path):
    cfg = Config({
        "nav_monitor": {
            "products": [
                {"provider": "citic_wealth", "code": "AF233276B", "name": "P1"},
            ]
        }
    })
    pending_path = tmp_path / "nav_pending.json"

    save_pending_nav_add(
        [ProductCandidate("citic_wealth", "AF233276B", "P1")],
        pending_path=str(pending_path),
        now=datetime(2026, 7, 8, 8, 0),
    )
    ok, message = confirm_pending_nav_add(
        cfg,
        1,
        pending_path=str(pending_path),
        now=datetime(2026, 7, 8, 8, 1),
    )

    assert ok is False
    assert "已存在" in message
    assert len(cfg.nav_monitor.products) == 1


def test_set_nav_product_shares_updates_existing_product():
    cfg = Config({
        "nav_monitor": {
            "products": [
                {"provider": "citic_wealth", "code": "AF233276B", "name": "P1"},
            ]
        }
    })

    ok, message = set_nav_product_shares(cfg, "AF233276B", Decimal("10000.5"))

    assert ok is True
    assert "已设置" in message
    assert cfg.nav_monitor.products[0].shares == Decimal("10000.5")


def test_set_nav_product_shares_batch_updates_matches_and_reports_failures():
    cfg = Config({
        "nav_monitor": {
            "products": [
                {"provider": "citic_wealth", "code": "AF233276B", "name": "P1"},
                {"provider": "citic_wealth", "code": "AF233262B", "name": "P2"},
            ]
        }
    })

    ok, message = set_nav_product_shares_batch(
        cfg,
        (
            ("AF233276B", Decimal("10000.5")),
            ("UNKNOWN", Decimal("3")),
            ("AF233262B", Decimal("20000")),
        ),
    )

    assert ok is True
    assert "成功**：2" in message
    assert "失败**：1" in message
    assert "P1：10000.5" in message
    assert "P2：20000" in message
    assert "UNKNOWN：未找到净值产品" in message
    assert cfg.nav_monitor.products[0].shares == Decimal("10000.5")
    assert cfg.nav_monitor.products[1].shares == Decimal("20000")


def test_scheduler_existing_times_do_not_include_nav_time():
    from src.scheduler import _get_times

    cfg = Config({
        "scheduler": {
            "cookie_check_hour": 9,
            "cookie_check_minute": 45,
            "report_submit_hour": 20,
            "report_submit_minute": 0,
            "stats_push_hour": 21,
            "stats_push_minute": 0,
            "cache_cleanup_hour": 4,
            "cache_cleanup_minute": 0,
        },
        "nav_monitor": {"push_hour": 8, "push_minute": 0},
    })

    assert _get_times(cfg) == (9, 45, 20, 0, 21, 0, 4, 0)


def test_scheduler_nav_monitor_uses_independent_time_state():
    from src.scheduler import _should_run_nav_monitor

    cfg = Config({"nav_monitor": {"enabled": True, "push_hour": 8, "push_minute": 0}})
    now = datetime(2026, 7, 8, 8, 0)

    assert _should_run_nav_monitor(cfg, now, None) is True
    assert _should_run_nav_monitor(cfg, now, "2026-07-08") is False

    cfg.nav_monitor.enabled = False
    assert _should_run_nav_monitor(cfg, now, None) is False


def test_scheduler_nav_monitor_runs_only_on_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _should_run_nav_monitor

    cfg = Config({"nav_monitor": {"enabled": True, "push_hour": 8, "push_minute": 0}})
    now = datetime(2026, 7, 11, 8, 0)

    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: False)

    assert _should_run_nav_monitor(cfg, now, None) is False


def test_scheduler_nav_evening_push_runs_on_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _should_run_nav_evening_push

    cfg = Config(
        {
            "nav_monitor": {
                "enabled": True,
                "evening_push_enabled": True,
                "evening_push_hour": 23,
                "evening_push_minute": 30,
            }
        }
    )
    now = datetime(2026, 7, 9, 23, 30)

    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: True)

    assert _should_run_nav_evening_push(cfg, now, None) is True
    assert _should_run_nav_evening_push(cfg, now, "2026-07-09") is False

    cfg.nav_monitor.evening_push_enabled = False
    assert _should_run_nav_evening_push(cfg, now, None) is False


def test_scheduler_nav_evening_push_runs_only_on_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _should_run_nav_evening_push

    cfg = Config(
        {
            "nav_monitor": {
                "enabled": True,
                "evening_push_enabled": True,
                "evening_push_hour": 23,
                "evening_push_minute": 30,
            }
        }
    )
    now = datetime(2026, 7, 11, 23, 30)

    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: False)

    assert _should_run_nav_evening_push(cfg, now, None) is False


def test_scheduler_finds_due_nav_periods_on_first_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _nav_period_push_jobs

    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: day.weekday() < 5)

    assert _nav_period_push_jobs(date(2026, 7, 6)) == [("week", date(2026, 7, 5))]
    assert _nav_period_push_jobs(date(2026, 7, 1)) == [
        ("month", date(2026, 6, 30)),
        ("quarter", date(2026, 6, 30)),
        ("half_year", date(2026, 6, 30)),
    ]
    assert _nav_period_push_jobs(date(2026, 1, 1)) == [
        ("month", date(2025, 12, 31)),
        ("quarter", date(2025, 12, 31)),
        ("half_year", date(2025, 12, 31)),
        ("year", date(2025, 12, 31)),
    ]


def test_scheduler_nav_weekly_period_push_rolls_to_first_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _nav_period_push_jobs

    holiday = date(2026, 7, 6)
    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: day.weekday() < 5 and day != holiday)

    assert _nav_period_push_jobs(date(2026, 7, 6)) == []
    assert _nav_period_push_jobs(date(2026, 7, 7)) == [("week", date(2026, 7, 5))]


def test_scheduler_nav_push_sends_due_periods_after_daily(monkeypatch):
    import src.portfolio_reports
    import src.scheduler
    from src.scheduler import _run_nav_monitor_push

    calls = []
    repository = object()

    def fake_push(
        cfg,
        repo,
        target_date=None,
        period="",
        to_user=None,
        holdings_as_of=None,
        disclosed_on=None,
        image_output_dir=None,
    ):
        calls.append((period or "daily", target_date, to_user, repo))
        return period or "daily"

    monkeypatch.setattr(
        src.scheduler,
        "_require_portfolio_repository",
        lambda: repository,
    )
    monkeypatch.setattr(src.portfolio_reports, "push_portfolio_report", fake_push)
    monkeypatch.setattr(
        src.scheduler,
        "_nav_period_push_jobs",
        lambda day: [("week", date(2026, 7, 5)), ("month", date(2026, 6, 30))],
    )

    _run_nav_monitor_push(Config({}), day=date(2026, 7, 6))

    assert calls == [
        ("daily", date(2026, 7, 6), "@all", repository),
        ("week", date(2026, 7, 5), "@all", repository),
        ("month", date(2026, 6, 30), "@all", repository),
    ]


def test_scheduler_nav_evening_push_calls_evening_report(monkeypatch):
    import src.portfolio_reports
    from src.scheduler import _run_nav_evening_push

    calls = []
    repository = object()

    def fake_push(
        cfg,
        repo,
        target_date=None,
        period="",
        to_user=None,
        holdings_as_of=None,
        disclosed_on=None,
        image_output_dir=None,
    ):
        calls.append((target_date, disclosed_on, to_user, repo))
        return "ok"

    monkeypatch.setattr(
        "src.scheduler._require_portfolio_repository",
        lambda: repository,
    )
    monkeypatch.setattr(src.portfolio_reports, "push_portfolio_report", fake_push)

    _run_nav_evening_push(
        Config({"wechat": {"to_user": "user1"}}),
        day=date(2026, 7, 9),
    )

    assert calls == [(date(2026, 7, 9), date(2026, 7, 9), "user1", repository)]


def test_scheduler_nav_estimate_runs_on_workday(monkeypatch):
    import src.scheduler
    from src.scheduler import _should_run_nav_estimate

    cfg = Config({"nav_monitor": {"estimate_enabled": True, "estimate_hour": 17, "estimate_minute": 30}})
    now = datetime(2026, 7, 9, 17, 30)

    monkeypatch.setattr(src.scheduler, "_is_workday", lambda day: True)

    assert _should_run_nav_estimate(cfg, now, None) is True
    assert _should_run_nav_estimate(cfg, now, "2026-07-09") is False


def test_scheduler_nav_estimate_push_updates_profiles_before_estimate(monkeypatch):
    import src.nav_holdings
    import src.portfolio_reports
    import src.scheduler
    from src.portfolio_models import ProductType
    from src.scheduler import _run_nav_estimate_push

    calls = []
    repository = object()
    wealth = type(
        "Product",
        (),
        {"code": "AF233276B", "product_type": ProductType.WEALTH_NAV},
    )()
    fund = type(
        "Product",
        (),
        {"code": "003103", "product_type": ProductType.PUBLIC_FUND},
    )()
    runtime = type("Runtime", (), {"repository": repository})()

    def fake_update(cfg, today=None, notify=False, products=None):
        calls.append(("update", today, notify, products))
        return []

    def fake_push(cfg, to_user=None, products=None):
        calls.append(("estimate", to_user, products))
        return "ok"

    monkeypatch.setattr(src.scheduler, "_portfolio_runtime", runtime)
    monkeypatch.setattr(
        src.portfolio_reports,
        "list_portfolio_position_products",
        lambda repo, as_of=None: [wealth, fund],
    )
    monkeypatch.setattr(src.nav_holdings, "refresh_quarterly_profiles_if_due", fake_update)
    monkeypatch.setattr(src.nav_holdings, "push_estimate_report", fake_push)

    _run_nav_estimate_push(Config({"wechat": {"to_user": "XiaXin"}}), day=date(2026, 7, 9))

    assert calls == [
        ("update", date(2026, 7, 9), False, [wealth]),
        ("estimate", "XiaXin", [wealth]),
    ]


def test_confirm_add_triggers_background_profile_refresh(monkeypatch, tmp_path):
    import src.nav_holdings as nav_holdings
    import src.nav_monitor as nav_monitor

    pending = tmp_path / "pending.json"
    cfg = Config({"nav_monitor": {"products": []}})
    candidate = ProductCandidate(
        provider="citic_wealth",
        code="AF233276B",
        name="慧盈象固收增强一年持有期5号B",
    )
    nav_monitor.save_pending_nav_add([candidate], pending_path=str(pending), now=datetime(2026, 7, 9, 10, 0))
    calls = []

    monkeypatch.setattr(
        nav_holdings,
        "refresh_profile_for_product",
        lambda cfg, product, notify=False: calls.append((product.code, notify)),
    )

    ok, message = nav_monitor.confirm_pending_nav_add(
        cfg,
        1,
        pending_path=str(pending),
        now=datetime(2026, 7, 9, 10, 1),
    )

    assert ok is True
    assert "已添加" in message
    assert calls == [("AF233276B", False)]


def test_scheduler_nav_push_exception_does_not_notify_report_failure(monkeypatch):
    import src.portfolio_reports
    import src.notifier
    from src.scheduler import _run_nav_monitor_push

    def fail_push(*_args, **_kwargs):
        raise RuntimeError("nav provider down")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("notify_report_failure should not be called")

    monkeypatch.setattr(
        "src.scheduler._require_portfolio_repository",
        lambda: object(),
    )
    monkeypatch.setattr(src.portfolio_reports, "push_portfolio_report", fail_push)
    monkeypatch.setattr(src.notifier, "notify_report_failure", fail_if_called)

    _run_nav_monitor_push(Config({}))
