from datetime import date, datetime
from decimal import Decimal

from src.beijing_time import TZ_CN
from src.fund_estimate_store import FundEstimateStore


def test_credit_ratings_are_separate_from_realtime_and_use_same_tenor_spreads(tmp_path):
    from src.fund_estimate_credit_eod import ChinaBondCreditSource
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    now = datetime(2026, 11, 2, 19, tzinfo=TZ_CN)
    def fetch(day):
        delta = Decimal('0.01') if day == date(2026, 10, 30) else Decimal(0)
        return {kind: {float(t): Decimal('2')+delta*multiplier for t in (1, 2, 3, 5, 10)}
                for kind, multiplier in [('rate_guozhai', 1), ('rate_guokai', 2),
                    ('credit_aaa', 3), ('credit_aa_plus', 4), ('credit_aa', 5)]}
    source = ChinaBondCreditSource(store, fetcher=fetch, clock=lambda: now)
    factor = source.fetch_daily_factors(date(2026, 10, 30), date(2026, 10, 30), source.source_family)[0]
    assert factor.source_family == 'chinabond_eod'
    assert factor.factor_set_id == 'bond_factor_credit_eod_v2'
    assert factor.values_bp['credit_aa_plus_spread_3y_bp'] == '3'
    assert factor.values_bp['credit_aa_spread_5y_bp'] == '4'
    assert factor.basis_date == '2026-10-29'
    assert factor.published_at == now.isoformat()
    assert factor.market_as_of == '2026-10-30T18:00:00+08:00'


def test_missing_rating_is_a_gap_and_cannot_be_filled_by_aaa(tmp_path):
    from src.fund_estimate_credit_eod import ChinaBondCreditSource
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    source = ChinaBondCreditSource(store, fetcher=lambda day: {},
        clock=lambda: datetime(2026, 11, 2, 19, tzinfo=TZ_CN))
    assert source.fetch_daily_factors(date(2026, 10, 30), date(2026, 10, 30), source.source_family) == []
