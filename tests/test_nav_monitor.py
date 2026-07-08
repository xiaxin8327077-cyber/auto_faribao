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
    build_add_product_candidates,
    calculate_change,
    confirm_pending_nav_add,
    format_product_candidates,
    format_nav_report,
    parse_nav_query_date,
    save_pending_nav_add,
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
