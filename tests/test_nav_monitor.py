from datetime import date, datetime
from decimal import Decimal

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
    save_pending_nav_add,
    set_nav_product_shares_batch,
    set_nav_product_shares,
)


def test_nav_monitor_config_defaults_to_enabled_with_0800():
    cfg = Config({})

    assert cfg.nav_monitor.enabled is True
    assert cfg.nav_monitor.push_hour == 8
    assert cfg.nav_monitor.push_minute == 0
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
    assert "### 1. 慧盈象固收增强一年持有期5号B（AF233276B）" in text
    assert "**机构**" not in text
    assert "**产品代码**" not in text
    assert "**最新净值**" not in text
    assert "**上期净值**" not in text
    assert "**涨跌幅**" not in text
    assert "净值：1.078000（2026-07-07）｜涨跌：<font color=\"info\">-0.000100（-0.0093%）</font>" in text


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
    assert "### 1. 慧盈象固收增强一年持有期5号B（AF233276B）" in text
    pad4 = "\u3000" * 4
    assert f"份额：10000{pad4}｜净值：1.078000（2026-07-07）｜涨跌：<font color=\"info\">-0.000100（-0.0093%）</font>｜收益：<font color=\"info\">-1.00 元</font>" in text
    assert "### 2. 慧盈象固收增强六个月持有期1号B（AF233262B）" in text
    assert f"份额：20000{pad4}｜净值：1.070800（2026-07-07）｜涨跌：<font color=\"warning\">0.000500（0.0467%）</font>｜收益：<font color=\"warning\">10.00 元</font>" in text


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

    pad8 = "\u3000" * 8
    pad4 = "\u3000" * 4
    assert f"份额：1{pad8}｜净值：1.000100（2026-07-07）" in text
    assert f"份额：20000{pad4}｜净值：1.000100（2026-07-07）" in text


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

    assert "## 📊 理财净值统计" in text
    assert "> **统计周期**：月度" in text
    assert "> **周期起点**：2026-07-01" in text
    assert "> **产品数量**" not in text
    assert "**机构**" not in text
    assert "### 慧盈象固收增强一年持有期5号B（AF233276B）" in text
    assert "**产品代码**" not in text
    assert "**期初净值**：2026-07-01  1.076700" in text
    assert "**净值变动**：<font color=\"warning\">0.001300（0.1207%）</font>" in text
    assert "**持仓份额**：10000" in text
    assert "**估算收益**：<font color=\"warning\">13.00 元</font>" in text


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


def test_scheduler_nav_push_exception_does_not_notify_report_failure(monkeypatch):
    import src.nav_monitor
    import src.notifier
    from src.scheduler import _run_nav_monitor_push

    def fail_push(cfg):
        raise RuntimeError("nav provider down")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("notify_report_failure should not be called")

    monkeypatch.setattr(src.nav_monitor, "push_nav_report", fail_push)
    monkeypatch.setattr(src.notifier, "notify_report_failure", fail_if_called)

    _run_nav_monitor_push(Config({}))
