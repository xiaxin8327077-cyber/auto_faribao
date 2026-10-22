from datetime import date
from decimal import Decimal

from src.bond_barometer import (
    _color,
    _curve_key,
    build_barometer,
    parse_curves,
)


def _curve(name, points):
    return {"ycDefName": name, "seriesData": [[t, str(v)] for t, v in points]}


def test_curve_name_mapping_prefers_fullwidth_plus_and_avoids_aa_capture():
    assert _curve_key("中债国债收益率曲线(到期)") == "rate_guozhai"
    assert _curve_key("中债国开债收益率曲线(到期)") == "rate_guokai"
    assert _curve_key("中债进出口行债收益率曲线(到期)") == "rate_jinchukou"
    assert _curve_key("中债中短期票据收益率曲线(AAA＋)(到期)") == "credit_aaa_plus"
    assert _curve_key("中债中短期票据收益率曲线(AAA)(到期)") == "credit_aaa"
    assert _curve_key("中债中短期票据收益率曲线(AA＋)(到期)") == "credit_aa_plus"
    assert _curve_key("中债中短期票据收益率曲线(AA)(到期)") == "credit_aa"
    assert _curve_key("未知曲线") is None


def test_parse_curves_reads_dense_tenor_points():
    payload = [
        _curve("中债国债收益率曲线(到期)", [(1.0, 1.1793), (3.0, 1.2653), (5.0, 1.3557), (7.0, 1.4554), (10.0, 1.6412)]),
        _curve("中债中短期票据收益率曲线(AA＋)(到期)", [(0.083333, 1.4227), (0.25, 1.43), (0.5, 1.44), (1.0, 1.4815), (3.0, 1.52), (5.0, 1.6938)]),
    ]
    curves = parse_curves(payload)
    assert curves["rate_guozhai"][1.0] == Decimal("1.1793")
    assert curves["credit_aa_plus"][0.0833] == Decimal("1.4227")


def test_color_threshold_and_direction():
    assert _color(Decimal("1.20"), Decimal("1.21")) == "red"
    assert _color(Decimal("1.21"), Decimal("1.20")) == "green"
    assert _color(Decimal("1.2001"), Decimal("1.2002")) == "flat"
    assert _color(None, Decimal("1.20")) == "flat"


def _fake_series(day, tenors, values):
    return {"seriesData": [[t, str(v)] for t, v in zip(tenors, values)]}


def test_build_barometer_computes_colors_and_walks_back_to_previous_trading_day():
    rate_tenors = [1.0, 3.0, 5.0, 7.0, 10.0]
    credit_tenors = [0.0833, 0.25, 0.5, 1.0, 3.0, 5.0]
    prev = {
        "rate_guozhai": dict(zip(rate_tenors, map(Decimal, ["1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "rate_guokai": dict(zip(rate_tenors, map(Decimal, ["1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "rate_jinchukou": dict(zip(rate_tenors, map(Decimal, ["1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "credit_aaa": dict(zip(credit_tenors, map(Decimal, ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "credit_aa_plus": dict(zip(credit_tenors, map(Decimal, ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "credit_aa": dict(zip(credit_tenors, map(Decimal, ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"]))),
    }
    # 2026-10-22 是周四；前一日 10-21（周三）应有数据。
    today = {
        "rate_guozhai": dict(zip(rate_tenors, map(Decimal, ["1.09", "1.2", "1.31", "1.4", "1.5"]))),
        "rate_guokai": dict(zip(rate_tenors, map(Decimal, ["1.1", "1.19", "1.3", "1.41", "1.49"]))),
        "rate_jinchukou": dict(zip(rate_tenors, map(Decimal, ["1.1", "1.2", "1.3", "1.4", "1.5"]))),
        "credit_aaa": dict(zip(credit_tenors, map(Decimal, ["1.0", "1.09", "1.2", "1.31", "1.4", "1.51"]))),
        "credit_aa_plus": dict(zip(credit_tenors, map(Decimal, ["1.01", "1.1", "1.19", "1.3", "1.41", "1.5"]))),
        "credit_aa": dict(zip(credit_tenors, map(Decimal, ["1.0", "1.1", "1.2", "1.3", "1.4", "1.51"]))),
    }
    calls = []

    def fake_fetch(day):
        calls.append(day)
        if day == date(2026, 10, 22):
            return today
        if day == date(2026, 10, 21):
            return prev
        return {}

    result = build_barometer(date(2026, 10, 22), fetch=fake_fetch)

    assert result["available"] is True
    assert result["as_of"] == "2026-10-22"
    assert result["previous_as_of"] == "2026-10-21"
    guozhai = result["rate"]["rows"][0]
    assert [cell["color"] for cell in guozhai["cells"]] == ["red", "flat", "green", "flat", "flat"]
    assert result["rate"]["counts"] == {"red": 3, "green": 2, "flat": 10}
    assert result["credit"]["counts"]["red"] == 2
    assert result["credit"]["counts"]["green"] == 5
    assert result["credit"]["weather"] in {"sunny", "sun_cloud", "cloud", "rain", "flat"}
    assert date(2026, 10, 21) in calls


def test_build_barometer_skips_weekend_and_empty_days():
    tenors = [1.0, 3.0, 5.0, 7.0, 10.0]
    base = {t: Decimal("1.2") for t in tenors}
    row = {"rate_guozhai": dict(base), "rate_guokai": dict(base), "rate_jinchukou": dict(base)}
    monday = date(2026, 10, 19)
    friday = date(2026, 10, 16)
    calls = []

    def fake_fetch(day):
        calls.append(day)
        return row if day in (monday, friday) else {}

    # 2026-10-18 是周日：应回退到 10-16（周五）作为基准，而不是空数据。
    result = build_barometer(monday, fetch=fake_fetch)
    assert result["previous_as_of"] == "2026-10-16"


def test_build_barometer_reports_unavailable_when_no_data():
    result = build_barometer(date(2026, 10, 22), fetch=lambda day: {})
    assert result == {"as_of": "2026-10-22", "available": False}

def test_latest_barometer_refreshes_only_during_trading_window(monkeypatch):
    import time
    from datetime import datetime

    import src.bond_barometer as module

    calls = []

    def fake_build(day):
        calls.append(day)
        return {"as_of": day.isoformat(), "available": True, "n": len(calls)}

    monkeypatch.setattr(module, "build_barometer", fake_build)
    module._cache.clear()
    day = date(2026, 10, 22)

    key = day.isoformat()
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 0))["n"] == 1
    # 30 分钟内的交易时段复用缓存
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 20))["n"] == 1
    # 缓存超过 30 分钟且仍在交易时段：自动刷新
    module._cache[key] = (time.monotonic() - 1900, module._cache[key][1])
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 40))["n"] == 2
    # 非交易时段：即使缓存过期也不自动刷新
    module._cache[key] = (time.monotonic() - 7200, module._cache[key][1])
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 22, 19, 0))["n"] == 2
    # 手动刷新随时有效
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 22, 19, 0), force=True)["n"] == 3
    # 周末不自动刷新（缓存仍有效）
    assert module.latest_barometer(day=day, now=datetime(2026, 10, 24, 10, 0))["n"] == 3
