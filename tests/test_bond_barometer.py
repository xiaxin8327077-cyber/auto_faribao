from datetime import date
from decimal import Decimal

import pytest

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

def test_latest_barometer_uses_database_snapshot_with_dedup_and_retention(monkeypatch):
    import sqlite3
    from datetime import datetime
    import src.bond_barometer as module

    calls = []

    def fake_build(day, fetch=None):
        calls.append(day)
        return {"as_of": day.isoformat(), "available": True, "n": len(calls)}

    monkeypatch.setattr(module, "fetch_live_barometer", lambda: fake_build(day), raising=False)
    monkeypatch.setattr(module, "build_barometer", fake_build)
    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    day = date(2026, 10, 22)

    # 无快照：抓取并落库
    first = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 0), conn=conn)
    assert first["n"] == 1
    assert first["snapshot_at"] == "2026-10-22T10:00:00"
    # 交易时段一分钟内：直接读快照
    second = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 0, 20), conn=conn)
    assert second["n"] == 1
    # 交易时段超过一分钟：重新抓取
    third = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 10, 1), conn=conn)
    assert third["n"] == 2
    # 非交易时段：直接用快照
    fourth = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 19, 0), conn=conn)
    assert fourth["n"] == 2
    # 强制刷新
    fifth = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 19, 5), force=True, conn=conn)
    assert fifth["n"] == 3
    # 当天内容一致：不新增行，刷新时间更新为最新
    before = conn.execute("SELECT COUNT(*) FROM bond_barometer_snapshots").fetchone()[0]
    module.write_snapshot(conn, {"as_of": "2026-10-22", "available": True, "n": 3}, datetime(2026, 10, 22, 19, 30))
    module.write_snapshot(conn, {"as_of": "2026-10-22", "available": True, "n": 3}, datetime(2026, 10, 22, 20, 0))
    rows = conn.execute(
        "SELECT payload, fetched_at, first_fetched_at FROM bond_barometer_snapshots ORDER BY id"
    ).fetchall()
    # 相同内容不新增行：首次时间保留（19:05 首次写入），最新刷新时间更新
    assert len(rows) == before
    assert rows[-1][1] == "2026-10-22T20:00:00"
    assert rows[-1][2] == "2026-10-22T19:05:00"
    assert '"n": 3' in rows[-1][0]
    fetched = module.latest_barometer(day=day, now=datetime(2026, 10, 22, 20, 5), conn=conn)
    assert fetched["snapshot_at"] == "2026-10-22T20:00:00"
    assert fetched["first_snapshot_at"] == "2026-10-22T19:05:00"
    # 内容变化：同一天保留新的一条
    module.write_snapshot(conn, {"as_of": "2026-10-22", "available": True, "n": 4}, datetime(2026, 10, 22, 21, 0))
    assert conn.execute("SELECT COUNT(*) FROM bond_barometer_snapshots").fetchone()[0] == before + 1
    # 30 天保留：超期快照被清理
    conn.execute(
        "INSERT INTO bond_barometer_snapshots(payload, as_of, fetched_at, fingerprint) VALUES(?,?,?,?)",
        ('{}', '', '2026-09-01T08:00:00', 'old'),
    )
    module.write_snapshot(conn, {"as_of": "2026-10-22", "available": True, "n": 5}, datetime(2026, 10, 22, 22, 0))
    kept = [row[0] for row in conn.execute("SELECT fetched_at FROM bond_barometer_snapshots")]
    assert "2026-09-01T08:00:00" not in kept
    assert all(value >= "2026-09-22T22:00:00" for value in kept)


def test_latest_snapshot_reads_most_recent_refresh_even_when_content_returns_to_old_value():
    import sqlite3
    from datetime import datetime
    from src.bond_barometer import ensure_barometer_schema, read_snapshot, write_snapshot

    conn = sqlite3.connect(":memory:")
    ensure_barometer_schema(conn)
    write_snapshot(conn, {"as_of": "2026-10-23", "available": True, "n": 1}, datetime(2026, 10, 23, 9, 30))
    write_snapshot(conn, {"as_of": "2026-10-23", "available": True, "n": 2}, datetime(2026, 10, 23, 9, 31))
    write_snapshot(conn, {"as_of": "2026-10-23", "available": True, "n": 1}, datetime(2026, 10, 23, 9, 32))
    assert read_snapshot(conn)["payload"]["n"] == 1
    assert conn.execute("SELECT COUNT(*) FROM bond_barometer_snapshots").fetchone()[0] == 2


def test_timestamp_only_source_updates_refresh_payload_without_duplicate_content():
    import sqlite3
    from datetime import datetime
    from src.bond_barometer import ensure_barometer_schema, read_snapshot, write_snapshot

    conn = sqlite3.connect(":memory:")
    ensure_barometer_schema(conn)
    payload = {"as_of": "2026-10-23", "available": True, "source_updated_at": "2026-10-23T09:30:00",
               "egg_estimates": {"source_updated_at": "2026-10-23T09:29:00", "items": []}}
    write_snapshot(conn, payload, datetime(2026, 10, 23, 9, 30))
    payload["source_updated_at"] = "2026-10-23T09:31:00"
    payload["egg_estimates"]["source_updated_at"] = "2026-10-23T09:31:00"
    write_snapshot(conn, payload, datetime(2026, 10, 23, 9, 31))
    assert conn.execute("SELECT COUNT(*) FROM bond_barometer_snapshots").fetchone()[0] == 1
    snapshot = read_snapshot(conn)["payload"]
    assert snapshot["source_updated_at"] == "2026-10-23T09:31:00"
    assert snapshot["first_snapshot_at"] == "2026-10-23T09:30:00"


def test_live_source_is_preferred_and_last_good_intraday_snapshot_survives_timeout(monkeypatch):
    import sqlite3
    from datetime import datetime
    import requests
    import src.bond_barometer as module

    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    live = {"as_of": "2026-10-23", "available": True, "source_id": "southernfund"}
    monkeypatch.setattr(module, "fetch_live_barometer", lambda: live.copy(), raising=False)

    def no_daily(_day):
        raise AssertionError("已有盘中数据不应改成昨天的日终数据")

    first = module.latest_barometer(now=datetime(2026, 10, 23, 9, 30), conn=conn, fetch=no_daily)
    assert first["source_id"] == "southernfund"

    def timeout():
        raise requests.Timeout("source timed out")

    monkeypatch.setattr(module, "fetch_live_barometer", timeout)
    result = module.latest_barometer(now=datetime(2026, 10, 23, 9, 32), conn=conn, fetch=no_daily)
    assert result["as_of"] == "2026-10-23"
    assert result["snapshot_at"] == "2026-10-23T09:30:00"
    assert result["refresh_error"]


def test_initial_live_failure_can_use_explicitly_marked_daily_fallback(monkeypatch):
    from datetime import datetime
    import requests
    import src.bond_barometer as module

    def unavailable():
        raise requests.Timeout("source timed out")

    monkeypatch.setattr(module, "fetch_live_barometer", unavailable, raising=False)
    curves = {"rate_guozhai": {1.0: Decimal("1.2")}}
    result = module.latest_barometer(
        now=datetime(2026, 10, 23, 9, 30),
        fetch=lambda day: curves if day in (date(2026, 10, 22), date(2026, 10, 21)) else {},
    )
    assert result["as_of"] == "2026-10-22"
    assert result["source_id"] == "chinabond"
    assert result["is_fallback"] is True


def test_same_second_snapshot_refreshes_preserve_subsecond_order():
    import sqlite3
    from datetime import datetime
    from src.bond_barometer import ensure_barometer_schema, read_snapshot, write_snapshot

    conn = sqlite3.connect(":memory:")
    ensure_barometer_schema(conn)
    a = {"as_of": "2026-10-23", "available": True, "n": 1}
    b = {**a, "n": 2}
    write_snapshot(conn, a, datetime(2026, 10, 23, 9, 30, 0, 100000))
    write_snapshot(conn, b, datetime(2026, 10, 23, 9, 30, 0, 200000))
    write_snapshot(conn, a, datetime(2026, 10, 23, 9, 30, 0, 300000))
    assert read_snapshot(conn)["payload"]["n"] == 1
    assert conn.execute("SELECT COUNT(*) FROM bond_barometer_snapshots").fetchone()[0] == 2


@pytest.mark.parametrize("stale_day,stale_time", [
    ("2026-10-22", "2026-10-22T17:00:00"),
    ("2026-10-23", "2026-10-23T09:59:00"),
])
def test_successful_but_older_upstream_response_cannot_replace_newer_snapshot(monkeypatch, stale_day, stale_time):
    import sqlite3
    from datetime import datetime
    import src.bond_barometer as module

    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    live = {"as_of": "2026-10-23", "available": True, "source_id": "southernfund",
            "source_updated_at": "2026-10-23T10:00:00", "n": 1}
    module.write_snapshot(conn, live, datetime(2026, 10, 23, 10, 0))
    monkeypatch.setattr(module, "fetch_live_barometer", lambda: {
        **live, "as_of": stale_day, "source_updated_at": stale_time, "n": 2,
    })
    result = module.latest_barometer(now=datetime(2026, 10, 23, 10, 2), conn=conn)
    assert result["as_of"] == "2026-10-23"
    assert result["source_updated_at"] == "2026-10-23T10:00:00"
    assert result["n"] == 1
    assert result["refresh_error"]
    assert module.read_snapshot(conn)["payload"]["n"] == 1


@pytest.mark.parametrize("incoming_eggs", [
    {"available": True, "as_of": "2026-10-23", "source_updated_at": "2026-10-23T09:59:00", "items": []},
    {"available": False, "items": []},
])
def test_new_matrix_preserves_more_recent_independently_timed_egg_estimate(monkeypatch, incoming_eggs):
    import sqlite3
    from datetime import datetime
    import src.bond_barometer as module

    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    live = {"as_of": "2026-10-23", "available": True, "source_id": "southernfund",
            "source_updated_at": "2026-10-23T10:00:00", "egg_estimates": {
                "available": True, "as_of": "2026-10-23", "source_updated_at": "2026-10-23T10:01:00", "items": [],
            }}
    module.write_snapshot(conn, live, datetime(2026, 10, 23, 10, 1))
    monkeypatch.setattr(module, "fetch_live_barometer", lambda: {
        **live, "source_updated_at": "2026-10-23T10:02:00", "egg_estimates": incoming_eggs,
    })
    result = module.latest_barometer(now=datetime(2026, 10, 23, 10, 3), conn=conn)
    assert result["source_updated_at"] == "2026-10-23T10:02:00"
    assert result["egg_estimates"]["source_updated_at"] == "2026-10-23T10:01:00"
    assert result["refresh_error"]


def test_refresh_rechecks_snapshot_written_while_upstream_request_was_running(monkeypatch):
    import sqlite3
    from datetime import datetime
    import src.bond_barometer as module

    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    live = {"as_of": "2026-10-23", "available": True, "source_id": "southernfund",
            "source_updated_at": "2026-10-23T10:00:00"}
    module.write_snapshot(conn, live, datetime(2026, 10, 23, 10, 0))

    def slow_upstream():
        # 模拟另一请求在本次联网过程中已发布了较新快照。
        module.write_snapshot(conn, {**live, "source_updated_at": "2026-10-23T10:02:00"},
                              datetime(2026, 10, 23, 10, 2))
        return {**live, "source_updated_at": "2026-10-23T10:01:00"}

    monkeypatch.setattr(module, "fetch_live_barometer", slow_upstream)
    result = module.latest_barometer(now=datetime(2026, 10, 23, 10, 1), conn=conn)
    assert result["source_updated_at"] == "2026-10-23T10:02:00"
    assert module.read_snapshot(conn)["payload"]["source_updated_at"] == "2026-10-23T10:02:00"


def test_completed_fetch_timestamp_orders_new_data_after_other_overlapping_refresh(monkeypatch):
    import sqlite3
    from datetime import datetime
    import src.bond_barometer as module

    conn = sqlite3.connect(":memory:")
    module.ensure_barometer_schema(conn)
    live = {"as_of": "2026-10-23", "available": True, "source_id": "southernfund", "n": 0,
            "source_updated_at": "2026-10-23T10:00:00"}
    module.write_snapshot(conn, live, datetime(2026, 10, 23, 10, 0))
    clock = iter([datetime(2026, 10, 23, 10, 1), datetime(2026, 10, 23, 10, 3)])
    monkeypatch.setattr(module, "beijing_now", lambda: next(clock))

    def upstream():
        module.write_snapshot(conn, {**live, "n": 1, "source_updated_at": "2026-10-23T10:02:00"},
                              datetime(2026, 10, 23, 10, 2))
        return {**live, "n": 2, "source_updated_at": "2026-10-23T10:03:00"}

    monkeypatch.setattr(module, "fetch_live_barometer", upstream)
    module.latest_barometer(conn=conn, force=True)
    assert module.read_snapshot(conn)["payload"]["source_updated_at"] == "2026-10-23T10:03:00"
    assert module.read_snapshot(conn)["payload"]["snapshot_at"] == "2026-10-23T10:03:00"
