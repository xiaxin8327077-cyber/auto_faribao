from datetime import date, datetime
from decimal import Decimal

from src.beijing_time import TZ_CN
from src.fund_estimate_cfets import CfetsCurveSource, CFETS_REALTIME
from src.fund_estimate_store import FundEstimateStore


NOW = datetime(2026, 11, 2, 10, tzinfo=TZ_CN)


def fetcher(kind, day, clock):
    shift = Decimal('0') if day < date(2026, 10, 30) else Decimal('0.01')
    rows = []
    for term in (1, 2, 3, 5, 10):
        # 不同期限、不同券种采用不同变化，手工期望可检验spread符号。
        multiplier = 1 if kind == 'gov' else 2 if kind == 'cdb' else 3
        value = Decimal('2')+shift*term*multiplier
        rows.append(['', '', '', str(term), '', '', str(value), ''])
    return {'data': {'date': day.isoformat(), 'time': clock, 'data': rows}}


def test_extended_tenors_keep_basis_units_and_legacy_features(tmp_path):
    from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource, EXTENDED_FACTOR_NAMES
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    old = CfetsCurveSource(store, fetcher=fetcher, clock=lambda: NOW)
    extended = ExtendedCfetsCurveSource(store, fetcher=fetcher, clock=lambda: NOW)
    legacy = old.fetch_daily_factors(date(2026, 10, 30), date(2026, 10, 30), CFETS_REALTIME)[0]
    result = extended.fetch_daily_factors(date(2026, 10, 30), date(2026, 10, 30), CFETS_REALTIME)[0]
    assert result.factor_set_id == 'bond_factor_v2'
    assert result.source_family == CFETS_REALTIME
    assert result.basis_date == '2026-10-29'
    assert result.values_bp['gov_1y_bp'] == '1'
    assert result.values_bp['gov_2y_bp'] == '2'
    assert result.values_bp['cdb_spread_10y_bp'] == '10'
    assert result.values_bp['credit_aaa_spread_5y_bp'] == '10'
    assert result.values_bp['credit_aaa_spread_10y_bp'] == '20'
    assert all(result.values_bp[name] == value for name, value in legacy.values_bp.items())
    assert set(result.values_bp) == set(EXTENDED_FACTOR_NAMES)
    assert result.source_version != legacy.source_version
    assert result.published_at == NOW.isoformat()


def test_missing_extra_tenor_is_not_zero_or_a_partial_observation(tmp_path):
    from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource, EXTENDED_FACTOR_NAMES
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    def missing(kind, day, clock):
        payload = fetcher(kind, day, clock)
        if kind == 'aaa':
            payload['data']['data'] = [r for r in payload['data']['data'] if r[3] != '1']
        return payload
    source = ExtendedCfetsCurveSource(store, fetcher=missing, clock=lambda: NOW)
    assert source.fetch_daily_factors(date(2026, 10, 30), date(2026, 10, 30), CFETS_REALTIME) == []
    live = source.read_intraday_factors(NOW)
    assert live.factor_set_id == 'bond_factor_v2'
    assert live.basis_status == 'missing'
    assert set(live.missing_fields) == set(EXTENDED_FACTOR_NAMES)


def test_extended_levels_do_not_reuse_the_partial_legacy_cache(tmp_path):
    from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    old = CfetsCurveSource(store, fetcher=fetcher, clock=lambda: NOW)
    old._levels(date(2026, 10, 30), '17:00')
    source = ExtendedCfetsCurveSource(store, fetcher=fetcher, clock=lambda: NOW)
    assert source._levels(date(2026, 10, 30), '17:00')['gov']['1.0'] == '2.01'
    cached = store.read_curve_levels(CFETS_REALTIME, '2026-10-30', '17:00', NOW.isoformat())
    assert '1.0' not in cached['levels']['gov']


def test_live_and_historical_extended_factors_use_identical_changes(tmp_path):
    from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    now = datetime(2026, 10, 30, 17, tzinfo=TZ_CN)
    source = ExtendedCfetsCurveSource(store, fetcher=fetcher, clock=lambda: now)
    history = source.fetch_daily_factors(now.date(), now.date(), CFETS_REALTIME)[0]
    live = source.read_intraday_factors(now)
    assert history.values_bp == live.values_bp
    assert live.basis_date == '2026-10-29'
    assert live.market_as_of == '2026-10-30T17:00:00+08:00'


def test_year_backfill_resolves_the_real_previous_day_across_2025_national_day():
    from src.portfolio_confirmation import is_trading_day
    assert is_trading_day(date(2025, 9, 30))
    assert not is_trading_day(date(2025, 10, 8))
    assert is_trading_day(date(2025, 10, 9))
    assert not is_trading_day(date(2025, 9, 28))  # 调休工作日也不能当成交易日。
