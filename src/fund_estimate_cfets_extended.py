"""同一实时曲线的多期限因子。只用已核实的国债、国开债、AAA中票。"""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

from src.fund_estimate_cfets import CFETS_REALTIME, CfetsCurveSource
from src.fund_estimate_types import FACTOR_NAMES, decimal_text


EXTENDED_FACTOR_NAMES = FACTOR_NAMES + (
    'gov_1y_bp', 'gov_2y_bp', 'gov_5y_bp',
    'cdb_spread_1y_bp', 'cdb_spread_3y_bp', 'cdb_spread_10y_bp',
    'credit_aaa_spread_1y_bp', 'credit_aaa_spread_5y_bp', 'credit_aaa_spread_10y_bp',
)
EXTENDED_FACTOR_UNITS = {name: 'day' if name == 'calendar_days' else 'bp' for name in EXTENDED_FACTOR_NAMES}


class ExtendedCfetsCurveSource(CfetsCurveSource):
    cache_family = CFETS_REALTIME + ':bond_factor_v2'
    factor_set_id = 'bond_factor_v2'
    factor_names = EXTENDED_FACTOR_NAMES
    required_terms = {'gov': (1., 2., 3., 5., 10.),
                      'cdb': (1., 3., 5., 10.), 'aaa': (1., 3., 5., 10.)}

    def _observation(self, day, quote_time, previous, current, base):
        observation = super()._observation(day, quote_time, previous, current, base)
        def delta(kind, term):
            key = str(float(term))
            return (Decimal(current[kind][key])-Decimal(base[kind][key]))*100
        values = dict(observation.values_bp)
        for term in (1, 2, 5):
            values[f'gov_{term}y_bp'] = decimal_text(delta('gov', term))
        for term in (1, 3, 10):
            values[f'cdb_spread_{term}y_bp'] = decimal_text(delta('cdb', term)-delta('gov', term))
        for term in (1, 5, 10):
            values[f'credit_aaa_spread_{term}y_bp'] = decimal_text(delta('aaa', term)-delta('gov', term))
        version = hashlib.sha256(json.dumps([
            self.factor_set_id, CFETS_REALTIME, day.isoformat(), quote_time, previous.isoformat(), values
        ], sort_keys=True).encode()).hexdigest()[:24]
        return replace(observation, factor_set_id=self.factor_set_id, values_bp=values, source_version=version)
