"""中债多评级日终研究因子。与CFETS实时口径隔离，不能替代盘中行情。"""
from datetime import datetime, time, timedelta
from decimal import Decimal
import hashlib
import json

import requests

from src.beijing_time import TZ_CN
from src.fund_estimate_cfets import CurveUnavailable
from src.fund_estimate_cfets_extended import EXTENDED_FACTOR_NAMES, ExtendedCfetsCurveSource
from src.fund_estimate_store import iso_now
from src.fund_estimate_types import FactorObservation, decimal_text, ensure_aware
from src.portfolio_confirmation import is_trading_day


CREDIT_EOD_NAMES = EXTENDED_FACTOR_NAMES + (
    'credit_aa_plus_spread_3y_bp', 'credit_aa_plus_spread_5y_bp',
    'credit_aa_spread_3y_bp', 'credit_aa_spread_5y_bp',
)


class ChinaBondCreditSource:
    source_family = 'chinabond_eod'
    factor_set_id = 'bond_factor_credit_eod_v2'
    factor_names = CREDIT_EOD_NAMES
    required_terms = dict(ExtendedCfetsCurveSource.required_terms, aa_plus=(3., 5.), aa=(3., 5.))
    kinds = {'gov': 'rate_guozhai', 'cdb': 'rate_guokai', 'aaa': 'credit_aaa',
             'aa_plus': 'credit_aa_plus', 'aa': 'credit_aa'}

    def __init__(self, store, *, fetcher=None, clock=None, calendar=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(TZ_CN))
        self.calendar = calendar or is_trading_day
        self.fetcher = fetcher or self._fetch
        self.session = None
        self._transient = set()

    def _fetch(self, day):
        from src.bond_barometer import _session, fetch_curves
        if self.session is None:
            self.session = _session()
        return fetch_curves(day, self.session)

    def _levels(self, day):
        cached = self.store.read_curve_levels(self.factor_set_id, day.isoformat(), '18:00', iso_now(self.clock()))
        if cached:
            return cached['levels']
        curves = self.fetcher(day)
        levels = {}
        for kind, key in self.kinds.items():
            points = curves.get(key) or {}
            if any(points.get(term) is None for term in self.required_terms[kind]):
                raise CurveUnavailable('中债日终评级/期限缺失')
            levels[kind] = {str(term): decimal_text(points[term]) for term in self.required_terms[kind]}
        self.store.save_curve_levels(self.factor_set_id, day.isoformat(), '18:00', levels, iso_now(self.clock()))
        return levels

    def fetch_daily_factors(self, start, end, source_family):
        if source_family != self.source_family:
            raise ValueError('中债日终不能冒充实时曲线')
        now = ensure_aware(self.clock(), TZ_CN)
        finish = min(end, now.date() if now.time() >= time(18) else now.date()-timedelta(days=1))
        self._transient = set()
        result, day = [], start
        while day <= finish:
            if self.calendar(day):
                previous = day-timedelta(days=1)
                while not self.calendar(previous):
                    previous -= timedelta(days=1)
                try:
                    current, base = self._levels(day), self._levels(previous)
                except (requests.RequestException, ValueError) as exc:
                    if not isinstance(exc, CurveUnavailable) or exc.transient:
                        self._transient.add(day)
                    day += timedelta(days=1)
                    continue
                def delta(kind, term):
                    return (Decimal(current[kind][str(float(term))])-Decimal(base[kind][str(float(term))]))*100
                gov3 = delta('gov', 3)
                values = dict(gov_3y_bp=decimal_text(gov3),
                    gov_slope_10y_3y_bp=decimal_text(delta('gov', 10)-gov3),
                    cdb_spread_5y_bp=decimal_text(delta('cdb', 5)-delta('gov', 5)),
                    credit_aaa_spread_3y_bp=decimal_text(delta('aaa', 3)-gov3),
                    calendar_days=str((day-previous).days))
                for term in (1, 2, 5):
                    values[f'gov_{term}y_bp'] = decimal_text(delta('gov', term))
                for term in (1, 3, 10):
                    values[f'cdb_spread_{term}y_bp'] = decimal_text(delta('cdb', term)-delta('gov', term))
                for kind, terms in [('aaa', (1, 5, 10)), ('aa_plus', (3, 5)), ('aa', (3, 5))]:
                    for term in terms:
                        values[f'credit_{kind}_spread_{term}y_bp'] = decimal_text(delta(kind, term)-delta('gov', term))
                version = hashlib.sha256(json.dumps([self.factor_set_id, day.isoformat(), previous.isoformat(), values],
                                                    sort_keys=True).encode()).hexdigest()[:24]
                stamp = iso_now(self.clock())
                result.append(FactorObservation(self.factor_set_id, self.source_family, day.isoformat(),
                    stamp, stamp, day.isoformat()+'T18:00:00+08:00', values, (day-previous).days,
                    'native', [], previous.isoformat(), version))
            day += timedelta(days=1)
        return result

    def take_transient_failures(self):
        pending, self._transient = self._transient, set()
        return pending
