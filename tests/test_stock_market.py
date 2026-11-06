"""Public-market cache tests: all HTTP is replaced at the transport boundary."""
from datetime import datetime
import sqlite3
import threading
import time

import pytest
import requests


def _module():
    import importlib
    try:
        return importlib.import_module("src.stock_market")
    except ModuleNotFoundError as exc:
        pytest.fail(f"stock-market cache has not been implemented: {exc}")


@pytest.mark.parametrize('stamp,active,next_open', [
    ('2026-10-27T20:00:00+08:00', True, '2026-10-27T13:00:00+00:00'),
    ('2026-10-27T12:15:00+08:00', True, '2026-10-27T05:00:00+00:00'),
    ('2026-11-10T20:00:00+08:00', True, '2026-11-10T13:00:00+00:00'),
    ('2026-10-25T20:00:00+08:00', False, '2026-10-26T01:00:00+00:00'),
])
def test_session_clock_sleeps_until_real_next_open_including_us_dst(stamp, active, next_open):
    calendar = {market: lambda day: day.weekday() < 5 for market in ('CN', 'HK', 'US')}
    session = _module().stock_market_session(_now(stamp), trading_calendars=calendar)
    assert session['any_active'] is active
    assert session['next_open_at'] == next_open
    assert session['next_transition_at'] == next_open


def test_session_clock_keeps_hk_active_during_cn_lunch():
    calendar = {market: lambda day: day.weekday() < 5 for market in ('CN', 'HK', 'US')}
    session = _module().stock_market_session(_now('2026-10-27T11:45:00+08:00'), trading_calendars=calendar)
    assert session['any_active'] is True
    assert session['next_transition_at'] == '2026-10-27T04:00:00+00:00'


def _now(value="2026-10-23T10:00:00+08:00"):
    return datetime.fromisoformat(value)


def _stamp(value="2026-10-23T09:59:48+08:00"):
    return int(_now(value).timestamp())


def _index_rows(stamp=None, price=3690.48):
    stamp = _stamp() if stamp is None else stamp
    return [
        {"f1": 2, "f2": price, "f3": 0.89, "f4": 32.45, "f12": "000001", "f13": 1, "f14": "上证指数", "f124": stamp},
        {"f1": 2, "f2": 3886.53, "f3": 2.44, "f4": 92.6, "f12": "HSTECH", "f13": 124, "f14": "恒生科技指数", "f124": stamp},
        {"f1": 2, "f2": 26008.86, "f3": -2.6, "f4": -694.0, "f12": "NDX", "f13": 100, "f14": "纳斯达克", "f124": _stamp("2026-10-22T16:00:00-04:00")},
    ]


def _industry_rows(stamp=None):
    stamp = _stamp() if stamp is None else stamp
    values = [("a", "计算机", 4.08), ("b", "机械", 3.01), ("c", "电子", 2.72),
              ("d", "传媒", 2.64), ("e", "汽车", 2.57), ("f", "电力设备", 2.31),
              ("g", "石油", -0.03), ("h", "公用事业", -0.22), ("i", "银行", -0.54),
              ("j", "平盘", 0), ("k", "缺值", "-")]
    return [{"f2": 100, "f3": pct, "f4": None, "f12": code, "f13": 90, "f14": name, "f124": stamp}
            for code, name, pct in values]


def _payload(rows):
    return {"rc": 0, "rt": 6, "full": 1, "data": {"total": len(rows), "diff": rows}}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, indices=None, industries=None, trends=None):
        self.indices = _payload(_index_rows()) if indices is None else indices
        self.industries = _payload(_industry_rows()) if industries is None else industries
        self.calls = []
        self.trends = trends

    def get(self, url, *, params, timeout, headers):
        self.calls.append((url, params.copy(), timeout))
        if '/trends2/' in url:
            result = self.trends
            if isinstance(result, Exception):
                raise result
            return _Response(result if result is not None else {'rc': 0, 'data': None})
        result = self.indices if "/ulist." in url else self.industries
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result()
        return _Response(result)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    _module().ensure_stock_market_schema(connection)
    yield connection
    connection.close()


def _index(snapshot, key):
    return next(item for item in snapshot["indices"] if item["key"] == key)


def test_read_empty_database_is_read_only_and_never_fetches(monkeypatch):
    module = _module()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA query_only=ON")
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("GET performed HTTP"))
    assert module.read_stock_market(connection, now=_now()) is None
    assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    connection.close()


def test_public_quotes_preserve_percentage_units_and_market_local_source_dates(conn):
    result = _module().refresh_stock_market(conn, now=_now(), session=_Session())
    assert len(result["indices"]) == 12
    shanghai = _index(result, "sse")
    assert shanghai["price"] == "3690.48"
    assert shanghai["change_pct"] == "0.89"
    assert shanghai["change_points"] == "32.45"
    nasdaq = _index(result, "nasdaq")
    assert nasdaq["name"] == "纳斯达克综合"
    assert nasdaq["quote_date"] == "2026-10-22"
    assert nasdaq["quote_time"] == "2026-10-22T16:00:00-04:00"
    assert nasdaq["timezone"] == "America/New_York"
    assert _index(result, "hstech")["available"] is True


@pytest.mark.parametrize("missing", ["-", None, "", "NaN", float("inf")])
def test_missing_fields_are_missing_and_never_coerced_to_zero(conn, missing):
    rows = _index_rows()
    rows[0]["f2"] = missing
    result = _module().refresh_stock_market(conn, now=_now(), session=_Session(indices=_payload(rows)))
    assert _index(result, "sse")["price"] is None
    assert _index(result, "sse")["available"] is False
    assert _index(result, "szse")["change_pct"] is None
    assert _index(result, "szse")["quote_date"] is None


def test_sector_ranking_uses_all_deduplicated_rows_and_only_actual_negative_values(conn):
    rows = _industry_rows()
    rows.insert(0, rows[0].copy())
    result = _module().refresh_stock_market(conn, now=_now(), session=_Session(industries=_payload(rows)))
    industries = result["industries"]
    assert [row["name"] for row in industries["leaders"]] == ["计算机", "机械", "电子", "传媒", "汽车"]
    assert [row["change_pct"] for row in industries["laggards"]] == ["-0.54", "-0.22", "-0.03"]
    assert industries["count"] == 11
    assert industries["quote_date"] == "2026-10-23"


def test_industry_request_uses_primary_industries_and_indices_request_uses_real_hstech_market(conn):
    session = _Session()
    _module().refresh_stock_market(conn, now=_now(), session=session)
    indices = next(params for url, params, _ in session.calls if "/ulist." in url)
    sectors = next(params for url, params, _ in session.calls if "/clist/" in url)
    assert "124.HSTECH" in indices["secids"]
    assert "100.HSTECH" not in indices["secids"]
    assert sectors["fs"] == "m:90+s:2+f:!50"
    assert sectors["pz"] >= 31


def test_ttl_skips_fresh_refresh_and_marks_expired_read_without_writes(conn):
    module = _module()
    session = _Session()
    module.refresh_stock_market(conn, now=_now(), session=session)
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-23T10:00:15+08:00"), session=session)
    assert session.calls == []
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    result = module.read_stock_market(conn, now=_now("2026-10-23T10:00:31+08:00"))
    assert _index(result, "sse")["stale"] is True
    assert module.stock_market_due(_now("2026-10-23T10:00:31+08:00"), result)


def test_active_market_collects_at_thirty_seconds(conn):
    module = _module()
    module.refresh_stock_market(conn, now=_now(), session=_Session())
    session = _Session()
    module.refresh_stock_market(conn, now=_now("2026-10-23T10:00:30+08:00"), session=session)
    assert any("/clist/" in url for url, _, _ in session.calls)


def test_intraday_curve_refreshes_with_quotes_and_stops_after_final_close(conn):
    module = _module()
    trend = {'rc': 0, 'data': {'market': 1, 'code': '000001', 'preClose': 3658.03,
                              'trends': ['2026-10-23 09:30,3623.57', '2026-10-23 09:59,3690.48']}}
    first = module.refresh_stock_market(conn, now=_now(), session=_Session(trends=trend))
    assert len(_index(first, 'sse')['trend']['points']) == 2
    session = _Session(trends=trend)
    module.refresh_stock_market(conn, now=_now('2026-10-23T10:00:30+08:00'), session=session)
    assert any('/trends2/' in url and params['secid'] == '1.000001' for url, params, _ in session.calls)
    module.refresh_stock_market(conn, now=_now('2026-10-23T16:01:00+08:00'), session=_Session(trends=trend))
    closed = _Session()
    module.refresh_stock_market(conn, now=_now('2026-10-23T17:00:00+08:00'), session=closed)
    assert not any('/trends2/' in url for url, _, _ in closed.calls)
    assert _index(module.read_stock_market(conn), 'sse')['trend']['points']


def test_existing_closed_cache_bootstraps_missing_curves_once(conn):
    import json

    module = _module()
    at = _now('2026-10-23T16:01:00+08:00')
    module.refresh_stock_market(conn, now=at, session=_Session())
    payload = json.loads(conn.execute("SELECT payload FROM stock_market_cache WHERE component='CN'").fetchone()[0])
    for quote in payload:
        quote.pop('trend', None)
    conn.execute("UPDATE stock_market_cache SET payload=? WHERE component='CN'", (json.dumps(payload),))
    conn.commit()
    assert module.stock_market_due(at, module.read_stock_market(conn, now=at))
    session = _Session()
    module.refresh_stock_market(conn, now=at, session=session)
    assert any('/trends2/' in url for url, _, _ in session.calls)
    session.calls.clear()
    module.refresh_stock_market(conn, now=at, session=session)
    assert session.calls == []


def test_refresh_failure_retains_snapshot_and_original_source_and_fetch_times(conn):
    module = _module()
    initial = module.refresh_stock_market(conn, now=_now(), session=_Session())
    failed = module.refresh_stock_market(
        conn, now=_now("2026-10-23T10:02:00+08:00"),
        session=_Session(indices=requests.Timeout("index timeout"), industries=requests.Timeout("sector timeout")),
    )
    assert failed["fetched_at"] == initial["fetched_at"]
    assert _index(failed, "sse")["quote_time"] == _index(initial, "sse")["quote_time"]
    assert failed["refresh_error"]
    assert failed["industries"]["refresh_error"]
    assert failed["available"] is True
    assert failed["stale"] is True
    assert module.read_stock_market(conn, now=_now("2026-10-23T10:02:01+08:00"))["refresh_error"]


def test_sector_failure_does_not_borrow_successful_index_fetch_date(conn):
    module = _module()
    module.refresh_stock_market(conn, now=_now("2026-10-22T10:00:00+08:00"), session=_Session())
    result = module.refresh_stock_market(conn, now=_now(), session=_Session(industries=ValueError("bad sector JSON")))
    assert result["industries"]["fetched_at"] == "2026-10-22T02:00:00+00:00"
    assert result["indices_status"]["CN"]["fetched_at"] == "2026-10-23T02:00:00+00:00"


def test_source_going_backwards_cannot_overwrite_newer_quotes(conn):
    module = _module()
    module.refresh_stock_market(conn, now=_now(), session=_Session())
    older = _Session(indices=_payload(_index_rows(_stamp("2026-10-22T15:00:00+08:00"), 1)),
                     industries=_payload(_industry_rows(_stamp("2026-10-22T15:00:00+08:00"))))
    result = module.refresh_stock_market(conn, now=_now("2026-10-23T10:02:00+08:00"), session=older)
    assert _index(result, "sse")["price"] == "3690.48"
    assert _index(result, "sse")["quote_date"] == "2026-10-23"
    assert result["industries"]["quote_date"] == "2026-10-23"
    assert result["refresh_error"]


def test_concurrent_older_result_does_not_overwrite_newer_refresh(tmp_path):
    module = _module()
    path = tmp_path / "market.db"
    setup = sqlite3.connect(path)
    module.ensure_stock_market_schema(setup)
    setup.close()
    started = threading.Event()
    release = threading.Event()
    errors = []

    def slow_indices():
        started.set()
        assert release.wait(5)
        return _payload(_index_rows(price=1))

    def old_refresh():
        try:
            with sqlite3.connect(path) as old_conn:
                module.refresh_stock_market(old_conn, now=_now(), session=_Session(indices=slow_indices))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=old_refresh)
    worker.start()
    assert started.wait(3)
    try:
        with sqlite3.connect(path) as new_conn:
            module.refresh_stock_market(new_conn, now=_now("2026-10-23T10:02:00+08:00"), session=_Session())
    finally:
        release.set()
        worker.join(5)
    assert not errors
    with sqlite3.connect(path) as connection:
        assert _index(module.read_stock_market(connection, now=_now()), "sse")["price"] == "3690.48"


def test_network_does_not_hold_a_database_write_transaction(tmp_path):
    module = _module()
    path = tmp_path / "market.db"
    connection = sqlite3.connect(path)
    module.ensure_stock_market_schema(connection)

    def check_database_unlocked():
        with sqlite3.connect(path, timeout=0.1) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        return _payload(_index_rows())

    assert module.refresh_stock_market(connection, now=_now(), session=_Session(indices=check_database_unlocked))["available"]
    connection.close()


def test_lunch_and_close_finalization_are_each_requested_only_once_even_when_forced(conn):
    module = _module()
    session = _Session()
    module.refresh_stock_market(conn, now=_now("2026-10-23T11:29:00+08:00"), session=session)
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-23T11:31:00+08:00"), session=session)
    assert any("/clist/" in url for url, _, _ in session.calls)
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-23T11:32:00+08:00"), session=session, force=True)
    assert not any("/clist/" in url for url, _, _ in session.calls)
    module.refresh_stock_market(conn, now=_now("2026-10-23T14:59:00+08:00"), session=session)
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-23T16:01:00+08:00"), session=session)
    assert session.calls
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-23T16:05:00+08:00"), session=session, force=True)
    assert session.calls == []
    module.refresh_stock_market(conn, now=_now("2026-10-23T20:00:00+08:00"), session=session, force=True)
    gold_calls = [call for call in session.calls if "/ulist." in call[0]]
    assert len(gold_calls) == 1
    assert gold_calls[0][1]["secids"] == "118.AU9999"
    session.calls.clear()
    # Friday's US close occurs on Saturday morning in Beijing, and gets one check.
    module.refresh_stock_market(conn, now=_now("2026-10-24T10:00:00+08:00"), session=session)
    quote_calls = [call for call in session.calls if '/ulist.' in call[0]]
    assert len(quote_calls) == 1
    assert "100.NDX" in quote_calls[0][1]["secids"]
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-24T11:00:00+08:00"), session=session, force=True)
    assert session.calls == []


def test_daytime_refresh_omits_closed_us_market_after_bootstrap(conn):
    module = _module()
    module.refresh_stock_market(conn, now=_now(), session=_Session())
    session = _Session()
    result = module.refresh_stock_market(conn, now=_now("2026-10-23T10:02:00+08:00"), session=session)
    secids = next(params["secids"] for url, params, _ in session.calls if "/ulist." in url)
    assert "100.NDX" not in secids
    assert _index(result, "nasdaq")["quote_date"] == "2026-10-22"


@pytest.mark.parametrize("value,us_due", [
    ("2026-07-06T21:29:00+08:00", False),
    ("2026-07-06T21:31:00+08:00", True),
    ("2026-12-07T21:31:00+08:00", False),
    ("2026-12-07T22:31:00+08:00", True),
])
def test_us_refresh_schedule_observes_new_york_daylight_saving_time(conn, value, us_due):
    module = _module()
    at = _now(value)
    module.refresh_stock_market(conn, now=at.replace(hour=18, minute=0), session=_Session())
    session = _Session()
    module.refresh_stock_market(conn, now=at, session=session)
    secids = ",".join(params.get("secids", "") for _, params, _ in session.calls)
    assert ("100.NDX" in secids) is us_due


def test_weekend_and_injected_market_holiday_do_not_poll_after_bootstrap(conn):
    module = _module()
    holiday = {"HK": lambda day: False, "US": lambda day: False}
    session = _Session()
    module.refresh_stock_market(conn, now=_now("2026-10-24T10:00:00+08:00"), session=session, trading_calendars=holiday)
    session.calls.clear()
    result = module.refresh_stock_market(conn, now=_now("2026-10-24T11:00:00+08:00"), session=session, force=True,
                                        trading_calendars=holiday)
    assert session.calls == []
    assert not module.stock_market_due(_now("2026-10-24T11:00:00+08:00"), result, trading_calendars=holiday)


def test_mainland_public_holiday_uses_existing_trading_calendar(conn):
    module = _module()
    session = _Session()
    module.refresh_stock_market(conn, now=_now("2026-10-01T10:00:00+08:00"), session=session)
    session.calls.clear()
    module.refresh_stock_market(conn, now=_now("2026-10-01T10:02:00+08:00"), session=session, force=True)
    assert not any("/clist/" in url for url, _, _ in session.calls)
    assert all("1.000001" not in params.get("secids", "") for _, params, _ in session.calls)


def test_overall_network_deadline_returns_without_waiting_for_stalled_transport(conn, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_NETWORK_BUDGET_SECONDS", 0.05)

    def stall():
        time.sleep(0.25)
        return _payload(_index_rows())

    started = time.monotonic()
    result = module.refresh_stock_market(conn, now=_now(), session=_Session(indices=stall))
    assert time.monotonic() - started < 0.2
    assert result["refresh_error"]


def test_industries_fetch_every_page_before_computing_leaders(conn):
    class PagedSession(_Session):
        def get(self, url, *, params, timeout, headers):
            if "/clist/" not in url:
                return super().get(url, params=params, timeout=timeout, headers=headers)
            self.calls.append((url, params.copy(), timeout))
            rows = _industry_rows()[:2] if params["pn"] == 1 else [
                {"f2": 100, "f3": 10, "f4": 1, "f12": "late", "f13": 90, "f14": "第二页", "f124": _stamp()},
            ]
            payload = _payload(rows)
            payload["data"]["total"] = 3
            return _Response(payload)

    result = _module().refresh_stock_market(conn, now=_now(), session=PagedSession())
    assert result["industries"]["count"] == 3
    assert result["industries"]["leaders"][0]["name"] == "第二页"


def test_a_new_collection_period_is_due_even_when_previous_period_is_fresh(conn):
    module = _module()
    module.refresh_stock_market(conn, now=_now("2026-10-23T12:59:59+08:00"), session=_Session())
    session = _Session()
    module.refresh_stock_market(conn, now=_now("2026-10-23T13:00:01+08:00"), session=session)
    assert any("/clist/" in url for url, _, _ in session.calls)


def test_cache_never_opens_network_inside_caller_write_transaction(conn):
    session = _Session()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="outside a write transaction"):
        _module().refresh_stock_market(conn, now=_now(), session=session)
    assert session.calls == []
    conn.rollback()


def test_source_timestamp_missing_is_not_filled_with_fetch_date(conn):
    rows = _index_rows()
    rows[0]["f124"] = "-"
    result = _module().refresh_stock_market(conn, now=_now(), session=_Session(indices=_payload(rows)))
    assert _index(result, "sse")["quote_date"] is None
    assert _index(result, "sse")["available"] is False


def test_eastmoney_index_failure_falls_back_to_tencent(conn):
    module = _module()

    lines = []
    for code, label, price, change, pct, stamp in (
        ('sh000001', '上证指数', '3690.48', '32.45', '0.89', '20261023161500'),
        ('hkHSI', '恒生指数', '23093.37', '158.69', '0.69', '2026/10/23 18:31:07'),
    ):
        fields = [''] * 33
        fields[1], fields[3], fields[30], fields[31], fields[32] = label, price, stamp, change, pct
        lines.append('v_' + code + '="' + '~'.join(fields) + '";\n')

    class Text:
        status_code = 200
        text = ''.join(lines)

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Session:
        def get(self, url, *, params, timeout, headers):
            if "qt.gtimg.cn" in url:
                return Text()
            if "/ulist." in url:
                raise requests.ConnectionError("eastmoney down")
            return _Response(_payload(_industry_rows()))

    result = module.refresh_stock_market(conn, now=_now(), session=Session())
    assert result["available"]
    assert _index(result, "sse")["price"] == "3690.48"
    assert _index(result, "hsi")["price"] == "23093.37"
