from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from src.beijing_time import TZ_CN


def curve(day='2026-10-22', time='17:00', shift=Decimal('0')):
    return {'head': {'rep_code': '200'}, 'data': {'date': day, 'time': time, 'data': [
        ['', '', '', str(term), 'true', '', '', ''] for term in (3, 5, 10)
    ] + [['', '', '', str(term), '', '', str(Decimal(value) + shift), '']
         for term, value in ((3, '1.2'), (5, '1.4'), (10, '1.6'))]}}


def test_standard_curve_uses_interpolated_mean_and_skips_placeholders():
    from src.fund_estimate_cfets import parse_curve
    points = parse_curve(curve(), date(2026, 10, 22), '17:00')
    assert points[3] == Decimal('1.2')
    assert points[10] == Decimal('1.6')


def test_historical_curve_rejects_latest_day_fallback_and_empty_payload():
    from src.fund_estimate_cfets import CurveUnavailable, parse_curve
    with pytest.raises(CurveUnavailable):
        parse_curve(curve(day='2026-10-23'), date(2026, 10, 22), '17:00')
    with pytest.raises(CurveUnavailable):
        parse_curve({'data': {'date': '2026-10-22', 'time': '17:00', 'data': []}}, date(2026, 10, 22), '17:00')


def test_history_and_intraday_use_identical_curve_method_and_basis(tmp_path):
    from src.fund_estimate_cfets import CfetsCurveSource, CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    calls = []
    def fetch(kind, day, time):
        calls.append((kind, day, time))
        shift = Decimal('0') if day == date(2026, 10, 21) else Decimal('0.01')
        if day == date(2026, 10, 23): shift = Decimal('0.02')
        return curve(day.isoformat(), time, shift)
    source = CfetsCurveSource(store, fetcher=fetch, clock=lambda: datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    history = source.fetch_daily_factors(date(2026, 10, 22), date(2026, 10, 22), CFETS_REALTIME)
    live = source.read_intraday_factors(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    assert history[0].source_family == live.source_family == CFETS_REALTIME
    assert history[0].basis_date == '2026-10-21'
    assert live.basis_date == '2026-10-22'
    assert history[0].values_bp['gov_3y_bp'] == live.values_bp['gov_3y_bp'] == '1'
    assert all(time == '17:00' for _, day, time in calls if day < date(2026, 10, 23))
    assert any(time == '10:00' for _, day, time in calls if day == date(2026, 10, 23))


def test_missing_previous_trading_day_never_compresses_return_interval(tmp_path):
    from src.fund_estimate_cfets import CfetsCurveSource, CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    def fetch(kind, day, time):
        p = curve(day.isoformat(), time)
        if day == date(2026, 10, 22): p['data']['data'] = []
        return p
    source = CfetsCurveSource(store, fetcher=fetch, clock=lambda: datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    assert source.fetch_daily_factors(date(2026, 10, 23), date(2026, 10, 23), CFETS_REALTIME) == []
    live = source.read_intraday_factors(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    assert live.basis_date is None
    assert live.missing_fields


def _resp(payload):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload
    return Resp()


class _LatestSession:
    """真实会话路径替身：无时点参数的请求返回最新时点，其余返回该时点曲线。"""

    def __init__(self, latest_date='2026-10-23', latest_time='17:00'):
        self.latest_date = latest_date
        self.latest_time = latest_time

    def get(self, url, *, params, headers, timeout):
        if not params.get('qttntime'):
            return _resp({'data': {'date': self.latest_date, 'time': self.latest_time}})
        return _resp(curve(params['qttndate'], params['qttntime']))


def _source(store, now, fetcher=None, session=None):
    from src.fund_estimate_cfets import CfetsCurveSource
    source = CfetsCurveSource(store, fetcher=fetcher,
                              clock=lambda: now,
                              calendar=lambda d: d.weekday() < 5)
    if session is not None:
        source.session = session
    return source


def test_close_snapshot_survives_past_five_minute_window(tmp_path):
    from src.fund_estimate_cfets import CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    now = datetime(2026, 10, 23, 17, 6, tzinfo=TZ_CN)
    source = _source(store, now, fetcher=lambda kind, day, time: curve(day.isoformat(), time))
    live = source.read_intraday_factors(now)
    # 17:00 是当日收盘定格值，17:06 后仍是合法参考，不能被 5 分钟门槛清空。
    assert live.basis_status == 'native'
    assert live.missing_fields == []
    assert live.market_as_of == '2026-10-23T17:00:00+08:00'
    assert live.values_bp['gov_3y_bp'] == '0'
    assert live.basis_date == '2026-10-22'


def test_close_reference_marks_curve_published_before_final_close(tmp_path):
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    now = datetime(2026, 10, 23, 17, 8, tzinfo=TZ_CN)
    source = _source(store, now, session=_LatestSession(latest_time='16:58'))
    live = source.read_intraday_factors(now)
    # 收盘后仍没有 17:00 最终曲线：保留最后可用时点并明确标注，不能冒充 17:00 收盘值。
    assert live.basis_status == 'close_reference'
    assert live.missing_fields == []
    assert live.market_as_of == '2026-10-23T16:58:00+08:00'
    assert live.basis_date == '2026-10-22'


def test_intraday_gap_over_five_minutes_remains_missing(tmp_path):
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    now = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)
    source = _source(store, now, session=_LatestSession(latest_time='09:50'))
    live = source.read_intraday_factors(now)
    # 盘中数据落后超过 5 分钟仍是不可用的陈旧值，保持 missing。
    assert live.basis_status == 'missing'
    assert live.missing_fields


def test_previous_day_curve_never_masquerades_as_today(tmp_path):
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    now = datetime(2026, 10, 23, 17, 6, tzinfo=TZ_CN)
    source = _source(store, now, session=_LatestSession(latest_date='2026-10-22', latest_time='17:00'))
    live = source.read_intraday_factors(now)
    assert live.basis_status == 'missing'
    assert live.as_of == '2026-10-23'


def test_transient_network_failure_is_kept_for_retry(tmp_path):
    from src.fund_estimate_cfets import CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()

    def fetch(kind, day, time):
        if day == date(2026, 10, 21):
            raise requests.ConnectionError('network down')
        return curve(day.isoformat(), time)

    source = _source(store, datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN), fetcher=fetch)
    items = source.fetch_daily_factors(date(2026, 10, 20), date(2026, 10, 23), CFETS_REALTIME)
    # 10-21 网络失败，10-22 因基准日抓不到也失败；两者都要留可重试水位。
    assert {item.as_of for item in items} == {'2026-10-20', '2026-10-23'}
    assert source.take_transient_failures() == {date(2026, 10, 21), date(2026, 10, 22)}
    assert source.take_transient_failures() == set()
    assert source.fetch_daily_factors(date(2026, 10, 19), date(2026, 10, 19), CFETS_REALTIME)
    assert source.take_transient_failures() == set()


def test_permanent_missing_history_is_not_queued_for_retry(tmp_path):
    from src.fund_estimate_cfets import CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()

    def fetch(kind, day, time):
        payload = curve(day.isoformat(), time)
        if day == date(2026, 10, 21):
            payload['data']['data'] = []
        return payload

    source = _source(store, datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN), fetcher=fetch)
    items = source.fetch_daily_factors(date(2026, 10, 20), date(2026, 10, 23), CFETS_REALTIME)
    # 来源本就没有的曲线数据不属于临时失败，不应无限重试。
    assert {item.as_of for item in items} == {'2026-10-20', '2026-10-23'}
    assert source.take_transient_failures() == set()


def test_conflicting_quotes_retry_but_fallback_missing_does_not(tmp_path):
    from src.fund_estimate_cfets import CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()

    def fetch(kind, day, time):
        if day == date(2026, 10, 20):
            return curve('2026-10-19', time)  # 请求时点无数据被接口回退
        if day == date(2026, 10, 21):
            payload = curve(day.isoformat(), time)
            payload['data']['data'].append(['', '', '', '3', '', '', '9.9', ''])  # 同期限冲突均值
            return payload
        return curve(day.isoformat(), time)

    source = _source(store, datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN), fetcher=fetch)
    items = source.fetch_daily_factors(date(2026, 10, 19), date(2026, 10, 23), CFETS_REALTIME)
    assert {item.as_of for item in items} == {'2026-10-19', '2026-10-23'}
    # 坏数据（冲突均值）留可重试水位；接口回退=该时点无数据，不进水位。
    assert source.take_transient_failures() == {date(2026, 10, 21), date(2026, 10, 22)}


def test_backoff_cache_hit_stays_transient_within_thirty_minutes(tmp_path):
    from src.fund_estimate_cfets import CFETS_REALTIME, CfetsCurveSource
    from src.fund_estimate_store import FundEstimateStore
    store = FundEstimateStore(tmp_path / 'e.db'); store.initialize()
    clock = [datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN)]

    def fetch(kind, day, time):
        raise requests.ConnectionError('network down')

    source = CfetsCurveSource(store, fetcher=fetch, clock=lambda: clock[0],
                              calendar=lambda d: d.weekday() < 5)
    assert source.fetch_daily_factors(date(2026, 10, 21), date(2026, 10, 21), CFETS_REALTIME) == []
    assert source.take_transient_failures() == {date(2026, 10, 21)}
    clock[0] += timedelta(minutes=10)
    assert source.fetch_daily_factors(date(2026, 10, 21), date(2026, 10, 21), CFETS_REALTIME) == []
    # 退避缓存内的重试失败仍是临时失败，不能被当成永久缺失放弃。
    assert source.take_transient_failures() == {date(2026, 10, 21)}
